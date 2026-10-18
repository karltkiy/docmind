#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Manual Compose wrapper for an ALREADY DEPLOYED contour.
#
# Why this exists: `docker compose -f docker-compose.prod.yml ps` in a fresh SSH
# session fails with
#
#   error while interpolating services.api.image: required variable
#   DOCMIND_IMAGE_REF is missing a value
#
# Compose resolves ${VAR:?...} from exactly three places - the process
# environment, --env-file, and a file literally named `.env` in the project
# directory. A service's `env_file:` is applied AFTER interpolation and never
# feeds it. The rollout exports DOCMIND_IMAGE_REF in its own process and also
# persists it to `.deployed_image_ref` (see remote-deploy.sh), but nothing loads
# that file automatically, so every manual command used to need a hand-written
# `set -a; . ./.deployed_image_ref; set +a`.
#
# This wrapper loads the state file, adds `--env-file .env.demo` in the demo
# contour, and forwards the remaining arguments to Compose unchanged:
#
#   ./.github/deploy/stack.sh ps
#   ./.github/deploy/stack.sh logs --tail=50 api worker
#   ./.github/deploy/stack.sh config --quiet
#   ./.github/deploy/stack.sh --profile seed run --rm demo-seed
#
# It never invents an image reference. If the state file is missing AND no
# running container can supply the digest, it refuses to run rather than let
# Compose bring something unpinned up.
#
# Invoke it through `bash` (the file arrives via scp, so its executable bit is
# not guaranteed): `bash .github/deploy/stack.sh ps`.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

log() { printf '[stack] %s\n' "$*" >&2; }
die() {
    log "$*"
    exit 1
}

# The contour is identified by the Compose file present next to the state
# files, not by the directory name, so the same script works in /opt/docmind
# and /opt/docmind-demo and survives a renamed DEPLOY_PATH.
COMPOSE_FILE=""
ENV_ARGS=()
API_CONTAINER=""
UI_CONTAINER=""

if [[ -f docker-compose.demo.yml ]]; then
    COMPOSE_FILE="docker-compose.demo.yml"
    # Mandatory on every demo invocation: `env_file:` in the Compose file only
    # fills the container environment, and ${VAR:?...} is resolved earlier.
    ENV_ARGS=(--env-file .env.demo)
    API_CONTAINER="docmind-demo-api-1"
    UI_CONTAINER="docmind-demo-demo-1"
    [[ -f .env.demo ]] || die "${ROOT_DIR}/.env.demo is missing; Compose cannot resolve the required variables."
elif [[ -f docker-compose.prod.yml ]]; then
    COMPOSE_FILE="docker-compose.prod.yml"
    API_CONTAINER="docmind-prod-api-1"
    [[ -f .env ]] || die "${ROOT_DIR}/.env is missing; Compose cannot resolve the required variables."
else
    die "no docker-compose.prod.yml or docker-compose.demo.yml in ${ROOT_DIR}; run this from a deployed contour directory."
fi

# Written by remote-deploy.sh after a healthy rollout. Safe to source: digest
# references are "repo@sha256:<hex>" with no whitespace and no $(...), unlike
# the Compose .env file, which is why the rollout keeps a separate shell-shaped
# file instead of telling operators to source .env.
if [[ -f .deployed_image_ref ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.deployed_image_ref
    set +a
fi

# Older deployments predate .deployed_image_ref. A running container still
# remembers the reference it was created with, so recover the digest from there.
# Anything that is not digest-pinned is rejected: a tag would silently point at
# a different build than the one that was verified during the rollout.
image_ref_from() {
    docker inspect --format '{{.Config.Image}}' "$1" 2>/dev/null || true
}

if [[ -z "${DOCMIND_IMAGE_REF:-}" ]]; then
    recovered="$(image_ref_from "${API_CONTAINER}")"
    if [[ "${recovered}" == *@sha256:* ]]; then
        DOCMIND_IMAGE_REF="${recovered}"
        export DOCMIND_IMAGE_REF
        log "recovered DOCMIND_IMAGE_REF from ${API_CONTAINER}"
    fi
fi

if [[ -n "${UI_CONTAINER}" && -z "${DOCMIND_DEMO_IMAGE_REF:-}" ]]; then
    recovered="$(image_ref_from "${UI_CONTAINER}")"
    if [[ "${recovered}" == *@sha256:* ]]; then
        DOCMIND_DEMO_IMAGE_REF="${recovered}"
        export DOCMIND_DEMO_IMAGE_REF
        log "recovered DOCMIND_DEMO_IMAGE_REF from ${UI_CONTAINER}"
    fi
fi

if [[ -z "${DOCMIND_IMAGE_REF:-}" ]]; then
    die "DOCMIND_IMAGE_REF is unknown: ${ROOT_DIR}/.deployed_image_ref is missing and ${API_CONTAINER} is not running. Re-run the rollout, or export DOCMIND_IMAGE_REF=repo@sha256:<digest> yourself."
fi
if [[ -n "${UI_CONTAINER}" && -z "${DOCMIND_DEMO_IMAGE_REF:-}" ]]; then
    die "DOCMIND_DEMO_IMAGE_REF is unknown: ${ROOT_DIR}/.deployed_image_ref is missing and ${UI_CONTAINER} is not running. Re-run the rollout, or export DOCMIND_DEMO_IMAGE_REF=repo@sha256:<digest> yourself."
fi

# Echo what was resolved, so a surprising `ps` output can be traced back to the
# contour and image it was executed against. Digest references are not secrets.
log "file=${COMPOSE_FILE} api=${DOCMIND_IMAGE_REF}"

exec docker compose -f "${COMPOSE_FILE}" "${ENV_ARGS[@]}" "$@"
