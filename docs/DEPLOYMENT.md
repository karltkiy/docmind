# DocMind — Deployment Guide (Zero to Production)

This guide walks a maintainer through deploying DocMind to a single VPS with the
hardened production stack ([`docker-compose.prod.yml`](../docker-compose.prod.yml))
and the automated rollout ([`.github/deploy/remote-deploy.sh`](../.github/deploy/remote-deploy.sh)).

> The production rollout is **approval-gated** and **digest-pinned**. Migrations
> are applied **once** by the rollout — never on container start — so a restart
> can never mutate the schema.

## 1. Prerequisites

| Requirement | Notes |
| --- | --- |
| VPS | 2+ vCPU / 4+ GB RAM (add ~8 GB if you run Ollama). Docker Engine + Compose v2. |
| DNS + TLS | A hostname pointing at the VPS; terminated by nginx/Caddy (see §6). |
| GitHub | Admin access to configure the `production` environment and secrets. |
| Registry | GHCR access to the image published by the `Release Image` workflow. |

## 2. Repository location on the server

The deploy workflow copies `docker-compose.prod.yml` and
`.github/deploy/remote-deploy.sh` into `DEPLOY_PATH` on every run, so the server
only needs the directory to exist:

```bash
sudo mkdir -p /opt/docmind && sudo chown "$USER" /opt/docmind
cd /opt/docmind
```

`DEPLOY_PATH` is where the server-side `.env` and the `.deployed_image_tag`
state file live; backups are written to `${DEPLOY_PATH}/backups`.

## 3. Server-side `.env`

Create `/opt/docmind/.env` (never committed — it is git-ignored). It must define
every value the production stack requires:

```dotenv
POSTGRES_USER=docmind
POSTGRES_PASSWORD=<strong-unique-password>
POSTGRES_DB=docmind

REDIS_PASSWORD=<strong-unique-redis-password>

API_KEY=<long-random-shared-secret>
CORS_ORIGINS=https://app.example.com

EMBEDDING_PROVIDER=openai
LLM_PROVIDER=openai
EMBEDDING_DIM=1536
OPENAI_API_KEY=<provider-key>

# Optional
ENABLE_DOCS=False
METRICS_ENABLED=True
BACKUP_SYNC_CMD=rclone copy --no-traverse
```

Rules enforced at rollout/boot:

- **Do not** set `DATABASE_URL` or `REDIS_URL` to a `localhost` URL — the deploy
  script rejects it so the Compose service hosts (`db`/`redis`) are used.
- `EMBEDDING_DIM` must match **both** the embedding model **and** the applied
  Alembic migration; the startup guard ([`src/db/schema_guard.py`](../src/db/schema_guard.py))
  refuses to boot on a mismatch.
- `CORS_ORIGINS` must be an explicit allow-list (never `*`).
- `API_KEY` is mandatory: the API refuses to start without it in production.

> Switching to Ollama: set `EMBEDDING_PROVIDER=ollama`, `LLM_PROVIDER=ollama`,
> `EMBEDDING_DIM=768`, then run Compose with `--profile ollama`. The rollout adds
> the profile automatically when a provider is `ollama`.

## 4. GitHub configuration

| Kind | Name | Purpose |
| --- | --- | --- |
| Environment | `production` | Required reviewers (manual approval gate) |
| Secret | `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_PORT`, `DEPLOY_PATH` | VPS access + repo location |
| Secret | `SSH_PASSPHRASE` | Passphrase for `SSH_KEY` when the key is encrypted; omit otherwise |
| Variable | `GHCR_USER` | Registry username; public, so it must **not** be a secret |
| Secret | `GHCR_PULL_TOKEN` | Pull the private image on the VPS |
| Variable | `PRODUCTION_URL` | Environment link shown in the Actions UI |

`SSH_PORT` defaults to `22`. If `SSH_HOST` is missing, the deploy job **fails
loudly** instead of silently skipping.

A passphrase-protected `SSH_KEY` is supported: both `appleboy/scp-action` and
`appleboy/ssh-action` take the passphrase from `SSH_PASSPHRASE`. With the secret
missing, drone-scp fails on `ssh: this private key is passphrase protected` and
then `ssh: unable to authenticate, attempted methods [none]` — an empty method
list means the key never loaded. Prefer an unencrypted deploy key
(`ssh-keygen ... -N ""`), which needs no extra secret.

## 5. First deploy

1. Merge to `main` (or push a `v*.*.*` tag). The `Release Image` workflow builds,
   Trivy-scans, and pushes to GHCR with SBOM + provenance.
2. Open the pending `Deploy Production` run and approve the `production`
   environment — or `workflow_dispatch` it with an explicit `image_tag`
   (e.g. `sha-<full-sha>`).
3. The rollout:
   - logs in to GHCR, pulls the tag, and resolves it to an immutable
     `repo@sha256:...` reference;
   - rejects a `localhost` `DATABASE_URL`/`REDIS_URL`;
   - starts `db`/`redis` (and `ollama` when required) and waits for health;
   - takes a **mandatory** `pg_dump` backup (the deploy aborts if it fails) and
     optionally copies it off-host via `BACKUP_SYNC_CMD`;
   - applies migrations **once**;
   - starts `api`/`worker` and waits for `/health`.

On the very first deploy there is no previous tag, so automatic rollback is
unavailable; the script logs this explicitly — be ready to intervene.

## 6. Reverse proxy (TLS + SSE)

Only the API is published, bound to `127.0.0.1:8000`. Terminate TLS in a reverse
proxy. SSE requires buffering to be disabled:

```nginx
location /api/v1/chat/completions {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # keep tokens streaming
    proxy_read_timeout 300s;
    add_header X-Accel-Buffering no;
}
```

A complete sample lives in [`deploy/nginx/docmind.conf.example`](../deploy/nginx/docmind.conf.example).
A Caddy equivalent — automatic TLS, the SSE route and the optional public demo
vhost in one file — lives in [`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example).

> **Forward the real client address.** The per-client rate limiter keys on the TCP
> peer address ([`src/api/deps.py`](../src/api/deps.py)). Behind a reverse proxy
> that address is the Docker gateway rather than the visitor, unless uvicorn is
> told to trust `X-Forwarded-For` from it. Set `FORWARDED_ALLOW_IPS` to the
> gateway range — and make the proxy *overwrite* the header, not append to it —
> so a single visitor's burst does not consume everyone's budget.

## 7. Verification

```bash
curl -fsS http://127.0.0.1:8000/health
# → {"status":"healthy", ..., "version":"1.0.0","environment":"production","provider_ready":true}

cat /opt/docmind/.deployed_image_tag   # should match the deployed tag
```

Protected routes:

```bash
curl -fsS -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/api/v1/documents
```

## 8. Rollback

If `/health` never turns healthy, the rollout redeploys the previous tag in
`.deployed_image_tag` — but **only when that image can resolve the current
database revision**. Otherwise it refuses and asks for manual intervention.

Schema migrations are **not** reverted: design them as **expand/contract**. To
restore the pre-deploy state, use the latest `${DEPLOY_PATH}/backups/predeploy-*.sql`.

## 9. Restore from backup

The rollout writes an **uncompressed** plain-SQL dump (`predeploy-*.sql`), so
there is nothing to gunzip. Because a plain `pg_dump` emits `CREATE TABLE`
without a matching `DROP`, restore into a **clean** database:

```bash
cd /opt/docmind
set -a; . ./.env; set +a
BACKUP=backups/predeploy-YYYYMMDDTHHMMSSZ.sql

# Stop writers so nothing changes mid-restore.
docker compose -f docker-compose.prod.yml stop api worker

# Recreate the database, then replay the dump.
docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\" WITH (FORCE);"'
docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$POSTGRES_DB\";"'
docker compose -f docker-compose.prod.yml exec -T db \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$BACKUP"

docker compose -f docker-compose.prod.yml up -d --no-build api worker
curl -fsS http://127.0.0.1:8000/health
```

`docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql`
is the exact inverse. The same procedure applies to the demo contour with
`docker-compose.demo.yml`, `.env.demo` and `backups-demo/`; a full walkthrough is
in [`docs/RUNBOOK.md`](RUNBOOK.md).

## 10. Operations checklist

- **Backups:** keep `BACKUP_SYNC_CMD` configured for off-host copies; run a
  periodic restore drill.
- **Metrics:** `/metrics` is exposed for scraping (Prometheus). Alert on
  `docmind_ready`, `/health`, ingestion failures, and chat error rate.
- **Logs:** single-line JSON on stderr; collect via your log stack.
- **Upgrades:** dependabot (pip / actions / docker) plus the weekly Trivy scan.
- **DB pool:** tune `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` so
  `(API workers + 1) * (pool + overflow)` stays under PostgreSQL `max_connections`.

## 11. Public demo contour (optional)

To show "[...] how it works [...]" on a marketing site, deploy a **second,
isolated** stack rather than exposing production: its own Compose project
(`name: docmind-demo`), its own database, its own `API_KEY` and its own rate and
spend limits. Uploads are disabled in the UI and the corpus is seeded from files
you chose.

- Compose file: [`docker-compose.demo.yml`](../docker-compose.demo.yml)
- Workflows: [`release-demo-image.yml`](../.github/workflows/release-demo-image.yml)
  (a Streamlit image, built separately so its CVEs cannot block production) and
  [`deploy-demo.yml`](../.github/workflows/deploy-demo.yml) (same rollout script,
  different `COMPOSE_FILE`, `ENV_FILE`, `DEPLOY_PATH` and `TRAFFIC_SERVICES`)
- Caddy vhost: the `demo.<domain>` block in
  [`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example)

Full walkthrough — server layout, `.env.demo`, seeding, iframe embed, budget
guardrails and isolation checks — is in [`docs/DEMO.md`](DEMO.md).
