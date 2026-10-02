# Security Policy

DocMind API is a production-grade Retrieval-Augmented Generation (RAG) service.
We take the security of our users' data and the integrity of the service
seriously. This document explains which versions we support, how to report a
vulnerability privately, and what you can expect from us in return.

> **Please do not report security vulnerabilities through public GitHub issues,
> discussions, or pull requests.**

## Supported Versions

We provide security fixes for the latest released version and the current
development branch. Older releases are no longer maintained.

| Version | Supported |
| --- | --- |
| `1.0.x` (latest release) | :white_check_mark: |
| `main` (development, pre-release) | :white_check_mark: Best effort |
| `< 1.0` | :x: |

If you are running a fork or an older tag, please reproduce the issue on
`main` or on the latest release before reporting.

## Reporting a Vulnerability

We use **GitHub Private Vulnerability Reporting** as our only intake channel.

1. Open the **Security** tab of the
   [`karltkiy/docmind`](https://github.com/karltkiy/docmind) repository.
2. Click **Report a vulnerability**.
3. Fill in the advisory form with the details described below.

This creates a private, encrypted-by-GitHub conversation visible only to the
maintainers until a fix is ready. If you cannot use this channel, you may open a
minimal public issue asking for a private contact — but do not include any
exploit details, proofs of concept, or sensitive data in that issue.

### What to Include

To help us triage quickly, please provide:

- **Affected version / commit** — the release tag or commit SHA you tested.
- **Component** — e.g. `documents` API, `chat`/SSE stream, ingestion worker,
  Docker/compose configuration, migrations, or configuration handling.
- **Provider configuration** — whether `EMBEDDING_PROVIDER` / `LLM_PROVIDER` was
  set to `openai` or `ollama`. Describe the setup, never include the actual
  credentials.
- **Reproduction steps** — a minimal, deterministic sequence, including the
  request payload and the observed vs. expected behaviour.
- **Proof of concept** — code, `curl` commands, or scripts (optional but very
  useful).
- **Impact** — what an attacker could achieve (data exposure, privilege
  escalation, denial of service, etc.).
- **Suggested fix** — if you have one (optional).

**Never include real secrets, API keys, database credentials, or personal data
in a report.** Redact them; we can request more detail privately if needed.

## Scope

The following are in scope for this policy:

- The HTTP API surface under [`src/api/v1/`](src/api/v1/) — upload, listing,
  status, deletion, and the SSE chat endpoint.
- Ingestion, parsing, chunking, embedding, and indexing performed by the worker.
- Handling of configuration and secrets, including the fail-fast startup logic
  in [`src/config.py`](src/config.py).
- Upload validation: allow-listing, size enforcement, and path handling.
- Vector search construction in [`src/services/vector_store.py`](src/services/vector_store.py).
- Container and deployment configuration: [`Dockerfile`](Dockerfile),
  [`docker-compose.yml`](docker-compose.yml),
  [`docker-compose.prod.yml`](docker-compose.prod.yml).
- Database models and Alembic migrations, including the `pgvector` extension and
  the HNSW index.
- Prompt-injection or retrieval-poisoning scenarios that lead to **data
  exfiltration, cross-tenant access, or execution of unintended actions**
  through the RAG pipeline.

### Out of Scope

- Vulnerabilities in third-party dependencies or base images. These should be
  reported upstream; we track them continuously via `pip-audit`, Dependabot, and
  Trivy (see [Security Practices](#security-practices)).
- Behaviour, availability, or data-handling policies of external LLM providers
  (OpenAI, Ollama) — including their training-data or retention practices.
- Issues that require a fully compromised host, cluster, or database
  credentials.
- Denial-of-service against a self-hosted deployment that lacks the documented
  production hardening (rate limiting, reverse proxy, resource limits).
- The absence of authentication/authorization in a local development or demo
  setup; the API is intended to be deployed behind an authenticating reverse
  proxy.
- Findings produced only by automated scanners without a demonstrated impact.
- Social engineering, physical attacks, and spam.

## Response Timeline

We aim to meet the following targets. They are good-faith best-effort goals for
an open-source project, not contractual guarantees.

| Stage | Target |
| --- | --- |
| Acknowledgement of the report | within 3 business days |
| Initial triage and severity assessment | within 7 business days |
| Fix or mitigation for critical / high severity | as soon as reasonably possible |
| Fix for lower severities | next scheduled release |

We will keep you updated throughout the process and may ask follow-up questions
to validate the report.

## Disclosure Policy

We follow **coordinated disclosure**:

- We will work with you privately until a fix is available.
- We ask that you do not publicly disclose the issue before a fix has been
  released, or before an agreed embargo date (typically up to 90 days).
- When the fix is published, we will credit you in the advisory and release
  notes unless you request to remain anonymous.

## Safe Harbor

We will not pursue or support legal action against researchers who:

- act in good faith and in accordance with this policy,
- test only against their own instances or explicitly authorised deployments,
- avoid privacy violations, data destruction, and service disruption,
- give us a reasonable amount of time to remediate before public disclosure.

If in doubt, contact us privately first via the channel above.

## Security Practices

DocMind already ships a number of controls that this policy builds upon:

- **Continuous scanning** in [`.github/workflows/security.yml`](.github/workflows/security.yml):
  CodeQL (`security-and-quality`), `pip-audit`, Dependency Review
  (`fail-on-severity: high`), gitleaks secret scanning, and Trivy filesystem
  scanning for `HIGH`/`CRITICAL` vulnerabilities and misconfigurations.
- **Weekly deep scans** (scheduled run) to catch newly disclosed advisories.
- **Automated dependency updates** via [`.github/dependabot.yml`](.github/dependabot.yml)
  for Python packages, GitHub Actions, and Docker images.
- **Hardened runtime**: multi-stage, non-root container image
  ([`Dockerfile`](Dockerfile)) and a production Compose stack that publishes no
  database or Redis ports and binds only the API to `127.0.0.1`
  ([`docker-compose.prod.yml`](docker-compose.prod.yml)).
- **Fail-fast, no-default secrets**: the app refuses to start without required
  credentials and keeps no in-code fallbacks ([`src/config.py`](src/config.py)).
- **Request-path protections**: upload allow-listing and size caps, opaque error
  payloads, `X-Request-ID` correlation, and structured JSON logging.

## Contact

All security matters are handled exclusively through **GitHub Private
Vulnerability Reporting** at
<https://github.com/karltkiy/docmind/security/advisories/new>.

No security email address is published. Please do not send vulnerability
details to individual maintainers through other channels.
