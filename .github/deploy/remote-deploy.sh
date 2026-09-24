#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Remote deployment executed on the production VPS over SSH by
# .github/workflows/deploy.yml.
#
# Guarantees:
#   * Pulls the immutable sha-tagged image produced by the release workflow.
#   * Backs up the database before applying migrations.
#   * Applies migrations before switching traffic.
#   * Verifies /health and automatically rolls back to the previous tag.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT_DIR}"

export DOCMIND_IMAGE IMAGE_TAG

log() { printf '[deploy] %s\n' "$*"; }

compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }

previous_tag="$(cat "${STATE_FILE}" 2>/dev/null || true)"
deploy_started=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
    log "rolling back to ${previous_tag}"
    IMAGE_TAG="${previous_tag}" \
        docker compose -f "${COMPOSE_FILE}" up -d --no-build
    if wait_for_health; then
        log "rollback to ${previous_tag} succeeded"
        printf '%s' "${previous_tag}" > "${STATE_FILE}"
        return 0
    fi
    log "rollback health check failed"
    return 1
}

on_error() {
    log "deployment failed (exit $?)"
    if [[ "${deploy_started}" == "1" ]]; then
        rollback || log "automatic rollback unsuccessful"
    fi
    exit 1
}
trap on_error ERR

# ---------------------------------------------------------------------------
# 1. Authenticate and pull the new images
# ---------------------------------------------------------------------------
log "logging in to the container registry"
printf '%s' "${GHCR_TOKEN}" \
    | docker login ghcr.io --username "${GHCR_USER}" --password-stdin >/dev/null

log "pulling ${DOCMIND_IMAGE}:${IMAGE_TAG}"
compose pull --quiet api worker

# ---------------------------------------------------------------------------
# 2. Ensure backing services are healthy
# ---------------------------------------------------------------------------
log "starting backing services"
compose up -d --no-build --wait db redis ollama

# ---------------------------------------------------------------------------
# 3. Back up the database before migrating
# ---------------------------------------------------------------------------
mkdir -p "${BACKUP_DIR}"
backup_file="${BACKUP_DIR}/predeploy-$(date -u +%Y%m%dT%H%M%SZ).sql"
if compose exec -T db \
    sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "${backup_file}" 2>/dev/null; then
    log "database backed up to ${backup_file}"
    # Retain only the most recent N backups.
    find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'predeploy-*.sql' \
        | sort -r | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
else
    log "database backup skipped (pg_dump unavailable)"
    rm -f "${backup_file}"
fi

# ---------------------------------------------------------------------------
# 4. Apply migrations and switch to the new image
# ---------------------------------------------------------------------------
log "applying database migrations"
compose run --rm --no-deps api alembic upgrade head

deploy_started=1
log "starting api and worker on ${IMAGE_TAG}"
compose up -d --no-build api worker ollama-init

# ---------------------------------------------------------------------------
# 5. Verify health (rolls back automatically on failure)
# ---------------------------------------------------------------------------
if ! wait_for_health; then
    rollback || true
    exit 1
fi

# Verify the running container matches the requested tag when a digest is given.
if [[ -n "${IMAGE_DIGEST:-}" ]]; then
    running_digest="$(docker inspect \
        --format '{{index .RepoDigests 0}}' "$(compose ps -q api)" 2>/dev/null || true)"
    if [[ "${running_digest}" != *"${IMAGE_DIGEST}"* ]]; then
        log "digest mismatch: expected ${IMAGE_DIGEST}, running ${running_digest}"
        rollback || true
        exit 1
    fi
    log "running digest verified against ${IMAGE_DIGEST}"
fi

# ---------------------------------------------------------------------------
# 6. Persist state for the next deployment / rollback
# ---------------------------------------------------------------------------
printf '%s' "${IMAGE_TAG}" > "${STATE_FILE}"
log "deployment complete: ${DOCMIND_IMAGE}:${IMAGE_TAG}"
