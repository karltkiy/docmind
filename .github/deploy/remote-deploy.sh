#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Remote deployment executed on the production VPS over SSH by
# .github/workflows/deploy.yml.
#
# Guarantees:
#   * Pulls the immutable image produced by the release workflow and pins the
#     stack to its digest (``repo@sha256:...``), not a mutable tag.
#   * Refuses to run against a server-side .env that points DATABASE_URL or
#     REDIS_URL at localhost (a common copy-paste mistake).
#   * Backs up the database before applying migrations and aborts if the backup
#     fails; optionally copies it off-host via BACKUP_SYNC_CMD.
#   * Starts the opt-in Ollama profile only when a provider needs it and waits
#     for the models before the API may report healthy.
#   * Applies migrations exactly once, before switching traffic.
#   * Verifies /health and only auto-rolls back when the previous image can
#     actually understand the current database revision.
# ---------------------------------------------------------------------------
set -euo pipefail

: "${GHCR_USER:?GHCR_USER is required}"
: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${DOCMIND_IMAGE:?DOCMIND_IMAGE is required}"
: "${IMAGE_TAG:?IMAGE_TAG is required}"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-36}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
STATE_FILE="${STATE_FILE:-.deployed_image_tag}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
ENV_FILE="${ENV_FILE:-.env}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT_DIR}"

export DOCMIND_IMAGE

log() { printf '[deploy] %s\n' "$*"; }

# Read a variable from the environment or the server-side .env file, so the
# rollout can decide whether the opt-in Ollama profile is required.
read_env_var() {
    local name="$1" fallback="${2:-}" value="${!name:-}"
    if [[ -z "${value}" && -f "${ENV_FILE}" ]]; then
        value="$(grep -E "^[[:space:]]*${name}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                  -e 's/[[:space:]]#.*$//' \
                  -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
    fi
    printf '%s' "${value:-${fallback}}"
}

EMBEDDING_PROVIDER="$(read_env_var EMBEDDING_PROVIDER openai)"
LLM_PROVIDER="$(read_env_var LLM_PROVIDER openai)"
OLLAMA_ENABLED=0
if [[ "${EMBEDDING_PROVIDER}" == "ollama" || "${LLM_PROVIDER}" == "ollama" ]]; then
    OLLAMA_ENABLED=1
fi

# Activate the opt-in Ollama profile only when a provider actually needs it.
# (Not named COMPOSE_PROFILES: that is a Docker Compose environment variable.)
PROFILE_ARGS=()
if [[ "${OLLAMA_ENABLED}" == "1" ]]; then
    PROFILE_ARGS=(--profile ollama)
fi

compose() {
    docker compose -f "${COMPOSE_FILE}" ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} "$@"
}

previous_tag="$(cat "${STATE_FILE}" 2>/dev/null || true)"
deploy_started=0

if [[ -z "${previous_tag}" ]]; then
    log "no previous deployment recorded: automatic rollback is unavailable for this run"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
assert_remote_dsn() {
    local name value
    for name in DATABASE_URL REDIS_URL; do
        value="$(read_env_var "${name}" "")"
        if [[ "${value}" == *"localhost"* || "${value}" == *"127.0.0.1"* ]]; then
            log "FATAL: ${name} points at localhost; remove it from ${ENV_FILE} so the"
            log "       Compose service hosts (db/redis) are used instead."
            exit 1
        fi
    done
}

image_ref_for_tag() {
    # Pull ``repo:tag`` and resolve its immutable ``repo@sha256:...`` reference.
    local tag="$1" ref
    docker pull "${DOCMIND_IMAGE}:${tag}" >/dev/null
    ref="$(docker image inspect --format '{{index .RepoDigests 0}}' \
        "${DOCMIND_IMAGE}:${tag}" 2>/dev/null || true)"
    printf '%s' "${ref:-${DOCMIND_IMAGE}:${tag}}"
}

current_db_revision() {
    compose run --rm --no-deps api alembic current 2>/dev/null \
        | awk 'NF {print $1}' | tail -n1
}

revision_known_to() {
    # Whether the image tagged ``$1`` can resolve database revision ``$2``.
    local tag="$1" revision="$2" ref
    ref="$(image_ref_for_tag "${tag}")"
    DOCMIND_IMAGE_REF="${ref}" compose run --rm --no-deps api alembic history 2>/dev/null \
        | grep -q -- "${revision}"
}

wait_for_health() {
    local attempt
    for ((attempt = 1; attempt <= HEALTH_RETRIES; attempt++)); do
        if curl -fsS "${HEALTH_URL}" \
            | grep -Eq '"status"[[:space:]]*:[[:space:]]*"healthy"'; then
            log "health check passed (attempt ${attempt}/${HEALTH_RETRIES})"
            return 0
        fi
        sleep "${HEALTH_INTERVAL}"
    done
    log "health check did not pass within $((HEALTH_RETRIES * HEALTH_INTERVAL))s"
    return 1
}

rollback() {
    if [[ -z "${previous_tag}" || "${previous_tag}" == "${IMAGE_TAG}" ]]; then
        log "no previous image tag available; manual intervention required"
        return 1
    fi

    # Never roll back onto an image that cannot understand the current schema:
    # alembic would fail with "Can't locate revision" and crash-loop the API.
    local revision
    # Never let a failed probe abort the script from inside the ERR trap.
    revision="$(current_db_revision || true)"
    if [[ -n "${revision}" ]] && ! revision_known_to "${previous_tag}" "${revision}"; then
        log "refusing automatic rollback: schema revision ${revision} is unknown to"
        log "${previous_tag}. Restore the pre-deploy backup or roll forward manually."
        return 1
    fi

    log "rolling back to ${previous_tag}"
    local old_ref
    old_ref="$(image_ref_for_tag "${previous_tag}")"
    DOCMIND_IMAGE_REF="${old_ref}" compose up -d --no-build
    if wait_for_health; then
        log "rollback to ${previous_tag} succeeded"
        printf '%s' "${previous_tag}" > "${STATE_FILE}"
        return 0
    fi
    log "rollback health check failed"
    return 1
}

on_error() {
    # Disable the trap first: a failure inside rollback must not re-enter this
    # handler recursively.
    trap - ERR
    log "deployment failed (exit $?)"
    if [[ "${deploy_started}" == "1" ]]; then
        rollback || log "automatic rollback unsuccessful"
    fi
    exit 1
}
trap on_error ERR

# ---------------------------------------------------------------------------
# 0. Reject a server-side .env that still points at localhost
# ---------------------------------------------------------------------------
assert_remote_dsn

# ---------------------------------------------------------------------------
# 1. Authenticate, pull the new image and pin it by digest
# ---------------------------------------------------------------------------
log "logging in to the container registry"
printf '%s' "${GHCR_TOKEN}" \
    | docker login ghcr.io --username "${GHCR_USER}" --password-stdin >/dev/null

log "resolving ${DOCMIND_IMAGE}:${IMAGE_TAG}"
DOCMIND_IMAGE_REF="$(image_ref_for_tag "${IMAGE_TAG}")"
export DOCMIND_IMAGE_REF
log "deploying ${DOCMIND_IMAGE_REF}"

# ---------------------------------------------------------------------------
# 2. Ensure backing services are healthy
# ---------------------------------------------------------------------------
backing_services=(db redis)
if [[ "${OLLAMA_ENABLED}" == "1" ]]; then
    backing_services+=(ollama)
fi
log "starting backing services: ${backing_services[*]}"
compose up -d --no-build --wait "${backing_services[@]}"

# ---------------------------------------------------------------------------
# 3. Back up the database before migrating
# ---------------------------------------------------------------------------
# A failed backup must abort the rollout: applying migrations without a safety
# net is worse than not deploying at all.
mkdir -p "${BACKUP_DIR}"
backup_file="${BACKUP_DIR}/predeploy-$(date -u +%Y%m%dT%H%M%SZ).sql"
if ! compose exec -T db \
    sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "${backup_file}"; then
    rm -f "${backup_file}"
    log "FATAL: database backup failed; refusing to migrate without a safety net"
    exit 1
fi
log "database backed up to ${backup_file}"

if [[ -n "${BACKUP_SYNC_CMD:-}" ]]; then
    # e.g. BACKUP_SYNC_CMD='rclone copy --no-traverse' → copies to off-host storage.
    log "syncing backup off-host"
    ${BACKUP_SYNC_CMD} "${backup_file}"
fi

# Retain only the most recent N backups locally.
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'predeploy-*.sql' \
    | sort -r | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f

# ---------------------------------------------------------------------------
# 3b. Ensure Ollama models exist before the API can report healthy
# ---------------------------------------------------------------------------
if [[ "${OLLAMA_ENABLED}" == "1" ]]; then
    log "pulling Ollama models (first run can take several minutes)"
    compose run --rm ollama-init
fi

# ---------------------------------------------------------------------------
# 4. Apply migrations once, then switch to the new image
# ---------------------------------------------------------------------------
log "applying database migrations"
compose run --rm --no-deps api alembic upgrade head

deploy_started=1
log "starting api and worker on ${IMAGE_TAG}"
compose up -d --no-build api worker

# ---------------------------------------------------------------------------
# 5. Verify health (rolls back automatically on failure)
# ---------------------------------------------------------------------------
if ! wait_for_health; then
    rollback || true
    exit 1
fi

# ---------------------------------------------------------------------------
# 6. Persist state for the next deployment / rollback
# ---------------------------------------------------------------------------
printf '%s' "${IMAGE_TAG}" > "${STATE_FILE}"
log "deployment complete: ${DOCMIND_IMAGE_REF}"
