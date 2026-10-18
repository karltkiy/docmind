# DocMind — Public Demo Contour

This document describes the second, **isolated** stack that serves the public
demo: a Streamlit UI on its own subdomain, embedded in the marketing site, backed
by its own database and its own API key. It runs beside production on the same
VPS without sharing anything that matters.

Compose files: [`docker-compose.demo.yml`](../docker-compose.demo.yml) (demo) and
[`docker-compose.prod.yml`](../docker-compose.prod.yml) (production).
Rollout: [`.github/workflows/deploy-demo.yml`](../.github/workflows/deploy-demo.yml)
reusing [`.github/deploy/remote-deploy.sh`](../.github/deploy/remote-deploy.sh).
For the full server-side procedure — from a bare VPS to a site-embedded demo —
see [`docs/RUNBOOK.md`](RUNBOOK.md) (written in Russian).

## 1. Why a separate contour

| Sharing the production stack would... | The demo contour instead... |
| --- | --- |
| Expose the production `API_KEY`, a single installation-wide secret ([`src/api/deps.py`](../src/api/deps.py)) | Uses its own `API_KEY`, valid only against the demo API |
| Put public uploads into the production corpus | Disables uploads in the UI and seeds a fixed corpus |
| Mix `EMBEDDING_DIM` (bound to the Alembic migration by [`src/db/schema_guard.py`](../src/db/schema_guard.py)) | Own database, own migration run |
| Spend the production OpenAI budget on anonymous traffic | Own budget limits, own rate limits, provider-side spend cap |
| Risk a demo rollout restarting production services | Own Compose project (`name: docmind-demo`), own networks and volumes |

Nothing is *physically* shared. `docker compose -f docker-compose.demo.yml down`
must never affect `docmind-prod`, because the project name differs.

## 2. Prerequisites

- The VPS already runs the production stack ([`docs/DEPLOYMENT.md`](DEPLOYMENT.md)).
- Capacity for a second, smaller stack. The demo caps itself at roughly
  1 vCPU / 1 GB for `api`, `worker` and `db` each, 256 MB for `redis` and 512 MB
  for the UI — about **4.5 GB RAM worst case**. Measure before enabling it, and
  trim the `deploy.resources.limits` in [`docker-compose.demo.yml`](../docker-compose.demo.yml)
  if production needs the headroom.
- DNS: `demo.<domain>` pointing at the VPS.
- The demo documents you are willing to show publicly.

## 3. Server layout

```bash
sudo mkdir -p /opt/docmind-demo/corpus
sudo chown -R "$USER" /opt/docmind-demo
```

`/opt/docmind-demo` holds everything the demo workflow touches:

| Path | Purpose |
| --- | --- |
| `.env.demo` | Demo secrets and settings — never committed, never shared with production |
| `corpus/` | The demo documents, mounted read-only into the seed service |
| `backups-demo/` | Pre-deploy `pg_dump` files (the same backup-before-migrate rule as production) |
| `.deployed_demo_image_tag` | State file used for rollback |
| `docker-compose.demo.yml`, `.github/deploy/remote-deploy.sh` | Copied by the workflow on every run |

### `.env.demo`

```dotenv
POSTGRES_USER=docmind_demo
POSTGRES_PASSWORD=<unique-demo-password>
POSTGRES_DB=docmind_demo

REDIS_PASSWORD=<unique-demo-redis-password>

# A demo-only key. Generate a fresh one and never reuse the production value:
# this key protects a public service, so treat it as disposable.
API_KEY=<unique-demo-api-key>

CORS_ORIGINS=https://demo.example.com
DOCMIND_PUBLIC_API_URL=https://demo.example.com

EMBEDDING_PROVIDER=openai
LLM_PROVIDER=openai
EMBEDDING_DIM=1536
OPENAI_API_KEY=<provider-key>

# Cost guardrails for a public deployment (defaults shown).
RATE_LIMIT_WINDOW_SECONDS=60
CHAT_RATE_LIMIT_REQUESTS=5
UPLOAD_RATE_LIMIT_REQUESTS=10
# Docker gateway range: without it every visitor looks like the same client and
# the rate limits become one shared budget for the whole internet.
FORWARDED_ALLOW_IPS=172.16.0.0/12

ENABLE_DOCS=False
METRICS_ENABLED=True
DOCMIND_DEMO_MAX_QUESTION_CHARS=500
DOCMIND_DEMO_TOP_K=4
SEED_WAIT_SECONDS=180
```

The values Compose refuses to start without are listed in
[`.env.example`](../.env.example) under "Public demo contour".

## 4. GitHub configuration

A dedicated environment keeps the demo away from the production secrets and,
because it has **no required reviewers**, does not gate on manual approval.

| Kind | Name | Value |
| --- | --- | --- |
| Environment | `demo` | No required reviewers |
| Secret | `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SSH_PORT` | Same VPS access |
| Secret | `SSH_PASSPHRASE` | Passphrase of the demo `SSH_KEY`; leave unset when the key is unencrypted |
| Secret | `DEPLOY_PATH` | `/opt/docmind-demo` — **must differ from production** |
| Secret | `GHCR_USER`, `GHCR_PULL_TOKEN` | Registry access |
| Variable | `DEMO_URL` | Public demo URL shown in the Actions UI |

`DEPLOY_PATH` is the one value that could silently damage production if it were
wrong: pointing the demo environment at `/opt/docmind` would make the demo
rollout rewrite files that the production rollout owns.

`SSH_PASSPHRASE` only exists for keys created **with** a passphrase; both
`appleboy/scp-action` and `appleboy/ssh-action` receive it through their
`passphrase` input. Without it drone-scp reports
`ssh: this private key is passphrase protected` followed by
`ssh: unable to authenticate, attempted methods [none]` — the empty method list
means the key was never loaded, so the fault is the missing passphrase, not the
server. An unencrypted deploy key is simpler and needs no extra secret.

## 5. Release and deploy flow

1. Push to `main` (or a `v*.*.*` tag).
2. `Release Demo Image` builds the same repository with
   `INSTALL_EXTRAS="[demo]"`, Trivy-scans it and pushes
   `ghcr.io/<owner>/<repo>-demo`. It is a **separate workflow on purpose**: a
   Streamlit dependency CVE must not be able to block a production release.
3. `Deploy Demo` runs automatically after a successful release (or manually via
   `workflow_dispatch` with an explicit `image_tag`), and on the VPS:
   - resolves the `api`/`worker` image and the demo UI image **by digest**;
   - starts `db`/`redis` and waits for health;
   - takes a mandatory `pg_dump` of the **demo** database;
   - applies migrations once;
   - brings up `api`, `worker` and `demo` (via `TRAFFIC_SERVICES`);
   - waits for `http://localhost:8001/health`;
   - runs the seed hook (`POST_DEPLOY_CMD`) and only then records the state file.

Manual equivalent on the server:

```bash
cd /opt/docmind-demo
export DOCMIND_IMAGE_REF=ghcr.io/<owner>/<repo>@sha256:<api-digest> \
       DOCMIND_DEMO_IMAGE_REF=ghcr.io/<owner>/<repo>-demo@sha256:<ui-digest>
docker compose -f docker-compose.demo.yml up -d --no-build
docker compose -f docker-compose.demo.yml --profile seed run --rm demo-seed
```

## 6. The demo corpus

`demo-seed` runs [`demo/seed_corpus.py`](../demo/seed_corpus.py) inside the demo
image. It is idempotent — a document already indexed (matched by filename, with
chunks) is skipped — and it waits for each upload to reach `completed`, exiting
non-zero on a failure or timeout so a broken corpus fails the rollout instead of
publishing an empty demo.

Supported suffixes: `.txt`, `.md`, `.csv`, `.json`, `.pdf`
([`src/services/parser.py`](../src/services/parser.py)). Put the files in
`/opt/docmind-demo/corpus` and re-run the seed service after adding more:

```bash
docker compose -f docker-compose.demo.yml --profile seed run --rm demo-seed
```

If a document is stuck in the `failed` state, the script stops rather than
uploading a duplicate. Remove it through the API, then re-run:

```bash
curl -X DELETE -H "X-API-Key: $DEMO_API_KEY" \
  http://127.0.0.1:8001/api/v1/documents/<document-id>
```

To start from a clean slate (drops the demo database and the seeded corpus):

```bash
docker compose -f docker-compose.demo.yml down
docker volume rm docmind-demo_demo_pg_data docmind-demo_demo_uploads
# next deploy re-creates the schema and re-seeds the corpus
```

## 7. Reverse proxy and TLS

[`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example) contains both
vhosts: the production API (SSE-aware, with `/metrics` restricted to internal
ranges) and the demo UI. Caddy obtains certificates automatically, so there is
nothing to renew by hand.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Two things matter for the demo vhost:

- **WebSockets.** Streamlit talks over `/_stcore/stream`; Caddy upgrades it
  transparently, but the timeouts must not kill an idle session
  (`read_timeout 0` in the sample).
- **Framing.** The site embeds the page in an iframe, so no `X-Frame-Options`
  header is set. To restrict who may frame it, use `frame-ancestors` instead of
  removing protection altogether — it is commented in the sample.

## 8. Embedding on the site

```html
<iframe
  src="https://demo.example.com/?embed=true"
  title="DocMind live demo"
  width="100%"
  height="820"
  style="border: 1px solid #d9d9d9; border-radius: 8px"
  loading="lazy"
  referrerpolicy="strict-origin-when-cross-origin"
  allow="clipboard-write"
></iframe>
<p>
  Устройство не отображает демонстрацию?
  <a href="https://demo.example.com/" target="_blank" rel="noopener">Откройте её в новой вкладке</a>.
</p>
```

Notes:

- `?embed=true` removes Streamlit's surrounding padding so the widget fits the
  frame. Verify it against the deployed Streamlit version.
- Always keep the fallback link: some corporate proxies and browser extensions
  block third-party frames, and CSP on the host page can block it too.
- Mixed content is a hard failure: an HTTPS site cannot frame an HTTP demo, which
  is another reason TLS terminates on the demo hostname itself.
- A browser loads the app **from the demo origin** and the page talks to the demo
  API **server-side**, so `CORS_ORIGINS` plays no part in the embed. It still has
  to be an explicit allow-list because the production posture refuses `*`.

## 9. Budget and abuse protection

- Set a hard **monthly spend limit** in the OpenAI account. The in-process limiter
  ([`src/api/deps.py`](../src/api/deps.py)) is per worker and only a speed bump;
  it is not a budget cap.
- `CHAT_RATE_LIMIT_REQUESTS` (default 5 per 60 s) is the per-visitor budget. The
  demo runs a single uvicorn worker precisely so the documented number is the real
  number.
- Uploads are unreachable from the UI, so `UPLOAD_RATE_LIMIT_REQUESTS` only
  throttles the seed run.
- `MAX_UPLOAD_MB` still applies to the upload route, so someone who obtains the
  key cannot push an arbitrarily large file.
- For a harder global limit than the application can provide, add
  `caddy-ratelimit` or put the demo hostname behind a CDN/WAF.

## 10. Monitoring

- `/metrics` is enabled and served **without authentication**
  ([`src/main.py`](../src/main.py)), so the Caddy sample restricts it by
  `remote_ip`. Scrape it over loopback or a private network only.
- Alert on `docmind_ready`, `/health`, chat error rate, and — because the demo
  spends money — on request volume per hour.
- Caddy writes a JSON access log per vhost with `client_ip`; the API logs
  single-line JSON with `X-Request-ID` ([`src/main.py`](../src/main.py)) for
  correlation.

## 11. Isolation verification

Run these after the first demo deploy. All four should hold:

```bash
# Demo key is rejected by the production API (and vice versa).
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "X-API-Key: $DEMO_API_KEY" https://api.example.com/api/v1/documents   # 401
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "X-API-Key: $PROD_API_KEY" http://127.0.0.1:8001/api/v1/documents     # 401

# Separate projects: neither stack can see the other's containers.
docker compose -f /opt/docmind/docker-compose.prod.yml ps
docker compose -f /opt/docmind-demo/docker-compose.demo.yml ps

# The demo database is not the production one.
docker compose -f /opt/docmind-demo/docker-compose.demo.yml exec db \
  psql -U "$POSTGRES_USER" -c '\dt' | head
```

## 12. Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Demo page shows "corpus is still empty" | The seed step never ran, or the corpus directory is empty; run `--profile seed run --rm demo-seed` and read its output |
| Seed exits with `FATAL: ... rate limited` | `UPLOAD_RATE_LIMIT_REQUESTS` is too low for the corpus size; the script retries using `Retry-After`, so raise the limit or accept the wait |
| Every visitor shares one rate limit | `FORWARDED_ALLOW_IPS` does not cover the Docker gateway, so uvicorn ignores `X-Forwarded-For` |
| Streamlit UI loads but never connects | WebSocket upgrade blocked in front of Caddy, or `read_timeout` too low on the demo vhost |
| Blank iframe | `X-Frame-Options`/`frame-ancestors` on the demo vhost, CSP on the hosting page, or mixed content |
| Deploy refuses to roll back | Expected on the very first demo deploy: there is no previous tag to return to |
