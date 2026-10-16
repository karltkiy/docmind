#!/usr/bin/env python3
"""Load the curated demo corpus into an isolated demo stack.

Runs inside the demo stack, where the ``[demo]`` extra already provides
``requests``:

    docker compose -f docker-compose.demo.yml --profile seed run --rm demo-seed

The script is idempotent: a document whose filename is already indexed is left
alone, so re-running it after a redeploy is a no-op. It exits non-zero when any
document fails to process or indexing times out, so the rollout notices instead
of silently leaving the public demo with an empty corpus.

Environment:
    CORPUS_DIR          directory with the demo documents (mounted read-only)
    DOCMIND_API_URL     internal API base URL, e.g. http://api:8000/api/v1
    DOCMIND_API_KEY     shared secret of the demo API
    SEED_WAIT_SECONDS   budget for background indexing (default 180)
    SEED_POLL_SECONDS   status polling interval (default 5)
"""

from __future__ import annotations

import mimetypes
import os
import sys
import time
from pathlib import Path

import requests

# Mirrors src/services/parser.py:ALLOWED_SUFFIXES. Kept as a local copy because
# this script runs against the HTTP API, not against the parser module.
ALLOWED_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".csv", ".json", ".pdf"})

API_URL = os.getenv("DOCMIND_API_URL", "http://localhost:8000/api/v1").rstrip("/")
API_KEY = os.getenv("DOCMIND_API_KEY", "")
CORPUS_DIR = Path(os.getenv("CORPUS_DIR", "/corpus"))
WAIT_SECONDS = int(os.getenv("SEED_WAIT_SECONDS", "180"))
POLL_SECONDS = int(os.getenv("SEED_POLL_SECONDS", "5"))

REQUEST_TIMEOUT = 60
# A 429 is expected when the corpus holds more files than the API allows per
# rate-limit window; wait as instructed (Retry-After) and retry.
MAX_RATE_LIMIT_RETRIES = 20


def log(message: str) -> None:
    """Emit a prefixed, immediately flushed progress line."""
    print(f"[seed] {message}", flush=True)


def auth_headers() -> dict[str, str]:
    """Return the API-key header when a key is configured."""
    return {"X-API-Key": API_KEY} if API_KEY else {}


def list_documents() -> list[dict]:
    """Return every document currently stored in the demo API."""
    response = requests.get(
        f"{API_URL}/documents", headers=auth_headers(), timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def document_status(document_id: str) -> dict:
    """Return the processing status of a single document."""
    response = requests.get(
        f"{API_URL}/documents/{document_id}/status",
        headers=auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def upload(path: Path) -> dict:
    """Upload one corpus file, waiting out any upload rate limit."""
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
        with path.open("rb") as handle:
            response = requests.post(
                f"{API_URL}/documents/upload",
                files={"file": (path.name, handle, content_type)},
                headers=auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
        if response.status_code == 202:
            return response.json()
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "60"))
            log(f"{path.name}: rate limited, waiting {wait}s (attempt {attempt})")
            time.sleep(wait)
            continue
        response.raise_for_status()
    raise RuntimeError(f"{path.name}: still rate limited after {MAX_RATE_LIMIT_RETRIES} attempts")


def wait_for_indexing(pending: dict[str, str]) -> int:
    """Block until every pending document finishes, returning an exit code."""
    deadline = time.monotonic() + WAIT_SECONDS
    while pending and time.monotonic() < deadline:
        for filename, document_id in list(pending.items()):
            try:
                info = document_status(document_id)
            except requests.RequestException as exc:
                log(f"{filename}: status check failed ({exc}), retrying")
                continue
            status = info.get("status")
            if status == "completed":
                log(f"{filename}: indexed ({info.get('chunk_count', 0)} chunks)")
                pending.pop(filename)
            elif status == "failed":
                log(f"FATAL: {filename} failed to process: {info.get('error')}")
                return 1
        if pending:
            time.sleep(POLL_SECONDS)

    if pending:
        log("FATAL: timed out waiting for indexing of: " + ", ".join(sorted(pending)))
        return 1
    return 0


def main() -> int:
    """Upload any missing corpus documents and wait until they are indexed."""
    if not CORPUS_DIR.is_dir():
        log(f"FATAL: corpus directory {CORPUS_DIR} does not exist")
        return 1

    files = sorted(
        path
        for path in CORPUS_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES
    )
    if not files:
        log(f"FATAL: no documents with a supported suffix found in {CORPUS_DIR}")
        return 1

    try:
        existing = {document["filename"]: document for document in list_documents()}
    except requests.RequestException as exc:
        log(f"FATAL: cannot reach the API at {API_URL}: {exc}")
        return 1

    pending: dict[str, str] = {}
    for path in files:
        known = existing.get(path.name)
        if known is not None:
            if known.get("chunk_count", 0) > 0:
                log(f"{path.name}: already indexed, skipping")
                continue
            if known.get("status") == "failed":
                # Re-uploading would create a duplicate entry; deleting is a
                # deliberate operator action, so fail loudly instead.
                log(f"FATAL: {path.name} is already stored in a failed state: {known.get('error')}")
                return 1
            log(f"{path.name}: already queued, waiting for indexing")
            pending[path.name] = known["id"]
            continue

        try:
            uploaded = upload(path)
        except (requests.RequestException, RuntimeError) as exc:
            log(f"FATAL: upload of {path.name} failed: {exc}")
            return 1
        log(f"{path.name}: uploaded as {uploaded['id']}")
        pending[path.name] = uploaded["id"]

    exit_code = wait_for_indexing(pending)
    if exit_code == 0:
        log("demo corpus ready")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
