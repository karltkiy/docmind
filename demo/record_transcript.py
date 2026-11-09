"""Record one real chat completion into a replayable transcript.

This is a **maintainer** tool, not part of the demo runtime. It performs exactly
one ``POST /api/v1/chat/completions`` (which costs provider tokens), captures the
SSE frames with their real timestamps, and writes one file that the demo can
replay for free forever after.

Usage::

    python -m demo.record_transcript \\
        --document refund-policy.md \\
        --question "How long does a refund take?" \\
        --id refund-window

The API key is read from the environment variable named by ``--api-key-env``
(default ``DOCMIND_API_KEY``). Its value is never printed, logged or written to
the transcript. If the key or the network is unavailable the command exits with
a readable error and writes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow ``python demo/record_transcript.py``
    sys.path.insert(0, str(REPO_ROOT))

from demo.replay import (  # noqa: E402
    INDEX_FILENAME,
    SCHEMA_VERSION,
    TRANSCRIPT_KIND,
    TranscriptError,
    validate_transcript,
)

#: Default output directory for recorded transcripts.
DEFAULT_OUTPUT_DIR = REPO_ROOT / "demo" / "fixtures" / "transcripts"

#: Default API base URL (matches the local development stack).
DEFAULT_BASE_URL = os.getenv("DOCMIND_API_URL", "http://localhost:8000/api/v1").rstrip("/")

#: Environment variable holding the API key by default.
DEFAULT_API_KEY_ENV = "DOCMIND_API_KEY"

#: Fixed diagnostic used when the credential is absent. Deliberately a constant
#: with no interpolation: printing a credential-adjacent variable name puts
#: sensitive-looking text on stderr and trips static analysis for clear-text
#: logging of sensitive information (CWE-532).
MISSING_KEY_MESSAGE = (
    "error: the API key environment variable is not set. The key is read from the "
    "environment, never printed and never written to the transcript. Use --api-key-env "
    "to point at a different variable name."
)

#: Transcript ids are file stems, so keep them filesystem-safe.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_SSE_PREFIX = "data: "

_REMINDER = "Recording costs one provider request; replaying this file is free."


class RecordTranscriptError(RuntimeError):
    """Raised when a recording cannot be produced; no file is written."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _requests_module() -> Any:
    """Import ``requests`` lazily so ``--help`` works without the demo extras."""
    try:
        import requests
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise RecordTranscriptError(
            "the 'requests' package is required to record a transcript; "
            'install the demo extras with `pip install -e ".[demo]"`.'
        ) from exc
    return requests


def _auth_headers(api_key: str) -> dict[str, str]:
    """Return the auth header; the value never leaves this mapping."""
    return {"X-API-Key": api_key} if api_key else {}


def _read_api_key(env_name: str) -> str | None:
    """Return the credential from ``env_name``, or ``None`` when it is blank.

    Kept in its own function so the value never flows into a log statement: the
    CLI layer only consumes the presence/absence result.
    """
    value = os.getenv(env_name, "").strip()
    return value or None


def _git_commit() -> str:
    """HEAD commit of the exporting checkout, or ``unknown`` outside git."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else "unknown"


def _env_or_unknown(name: str) -> str:
    value = os.getenv(name, "").strip()
    return value or "unknown"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_sse_line(line: str | bytes) -> dict[str, Any] | None:
    """Decode one ``data:`` line into a frame dictionary.

    Comment/keep-alive lines return ``None``; a malformed payload raises a
    readable error instead of being skipped silently.
    """
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text or not text.startswith(_SSE_PREFIX):
        return None
    body = text[len(_SSE_PREFIX) :].strip()
    if not body or body == "[DONE]":
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RecordTranscriptError(
            f"the stream contained a malformed SSE payload: {exc.msg} (line {exc.lineno})"
        ) from exc
    if not isinstance(payload, dict):
        raise RecordTranscriptError("the stream contained a non-object SSE payload")
    return payload


def _fetch_chunk_count(
    base_url: str, filename: str, headers: dict[str, str], timeout: float
) -> int | None:
    """Best-effort chunk count for the recorded document (a plain GET, no model)."""
    requests = _requests_module()
    try:
        response = requests.get(f"{base_url}/documents", headers=headers, timeout=timeout)
        response.raise_for_status()
        documents = response.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(documents, list):
        return None
    for document in documents:
        if isinstance(document, dict) and document.get("filename") == filename:
            try:
                return int(document.get("chunk_count", 0))
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# Capture and transcript assembly
# ---------------------------------------------------------------------------
def capture(
    *,
    base_url: str,
    question: str,
    document: str,
    top_k: int,
    api_key: str,
    timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Perform the single streaming chat request and return frames + timings.

    Raises:
        RecordTranscriptError: The request failed or the stream did not end with
            a ``done`` frame. Nothing has been written to disk at this point.
    """
    requests = _requests_module()
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int(round((time.perf_counter() - started) * 1000))

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            json={"query": question, "top_k": top_k},
            headers=_auth_headers(api_key),
            stream=True,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RecordTranscriptError(
            f"could not reach {base_url}/chat/completions ({exc.__class__.__name__})"
        ) from exc

    status = getattr(response, "status_code", 200)
    if status >= 400:
        # The response body is deliberately not echoed: it is untrusted mirror
        # output and may contain request headers, including the credential.
        raise RecordTranscriptError(f"the API answered HTTP {status}")

    frames: list[dict[str, Any]] = []
    retrieval_ms: int | None = None
    first_token_ms: int | None = None
    token_parts: list[str] = []

    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            payload = _parse_sse_line(raw_line)
            if payload is None:
                continue
            frame_type = payload.get("type")
            t_ms = elapsed_ms()
            if frame_type == "sources":
                content = payload.get("content") or []
                if not isinstance(content, list):
                    raise RecordTranscriptError("the 'sources' frame did not carry a list")
                sources: list[dict[str, Any]] = []
                for item in content:
                    if not isinstance(item, dict):
                        raise RecordTranscriptError("a source entry was not an object")
                    sources.append(
                        {
                            "filename": str(item.get("filename") or ""),
                            "chunk_index": int(item.get("chunk_index", 0)),
                            "score": float(item.get("score", 0.0)),
                            "excerpt": str(item.get("excerpt") or ""),
                        }
                    )
                frames.append({"t_ms": t_ms, "type": "sources", "content": sources})
                retrieval_ms = t_ms if retrieval_ms is None else retrieval_ms
            elif frame_type == "token":
                content = payload.get("content")
                if not isinstance(content, str):
                    raise RecordTranscriptError("the 'token' frame did not carry a string")
                token_parts.append(content)
                frames.append({"t_ms": t_ms, "type": "token", "content": content})
                first_token_ms = t_ms if first_token_ms is None else first_token_ms
            elif frame_type == "error":
                message = str(payload.get("message") or "unknown error")[:200]
                raise RecordTranscriptError(f"the API returned an error frame: {message}")
            elif frame_type == "done":
                frames.append({"t_ms": t_ms, "type": "done"})
                break
    except requests.RequestException as exc:
        raise RecordTranscriptError(
            f"the stream broke while reading it ({exc.__class__.__name__})"
        ) from exc

    if not frames or frames[-1]["type"] != "done":
        raise RecordTranscriptError(
            "the stream ended without a 'done' frame; no transcript was written"
        )

    total_ms = frames[-1]["t_ms"]
    first_token = first_token_ms if first_token_ms is not None else total_ms
    timings = {
        "retrieval": float(retrieval_ms if retrieval_ms is not None else 0),
        "first_token": float(first_token),
        "stream": float(max(0, total_ms - first_token)),
        "frames": float(total_ms),
    }
    return frames, timings


def build_transcript(
    *,
    transcript_id: str,
    question: str,
    document: str,
    frames: list[dict[str, Any]],
    timings_ms: dict[str, float],
    chunk_count: int | None,
    top_k: int,
) -> dict[str, Any]:
    """Assemble the transcript document from a captured stream.

    Provider/model fields come from the environment when available; otherwise
    they are ``"unknown"``. Nothing is guessed.
    """
    token_text = "".join(
        str(frame.get("content", "")) for frame in frames if frame.get("type") == "token"
    )
    notes: list[str] = []
    if chunk_count is None:
        chunk_count = 0
        notes.append("document chunk_count is unknown: the API listing was unavailable")
    embedding_dim = _env_int("EMBEDDING_DIM", 0)
    if embedding_dim <= 0:
        notes.append("embedding_dim is 0: EMBEDDING_DIM was not set when recording")
    if timings_ms.get("retrieval", 0.0) == 0.0:
        notes.append("retrieval time is 0: no 'sources' frame was observed")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": TRANSCRIPT_KIND,
        "id": transcript_id,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "question": question,
        "provenance": {
            "git_commit": _git_commit(),
            "source": "live capture: POST /api/v1/chat/completions",
            "llm_provider": _env_or_unknown("LLM_PROVIDER"),
            "llm_model": _env_or_unknown("LLM_MODEL"),
            "embedding_model": _env_or_unknown("EMBEDDING_MODEL"),
            "embedding_dim": embedding_dim,
            "top_k": top_k,
        },
        "document": {"filename": document, "chunk_count": chunk_count},
        "frames": frames,
        "answer_sha256": hashlib.sha256(token_text.encode("utf-8")).hexdigest(),
        "timings_ms": timings_ms,
        "notes": notes,
    }


def update_index(directory: Path, *, document: str, transcript_id: str) -> Path:
    """Idempotently map ``document -> transcript_id`` in ``index.json``."""
    index_path = directory / INDEX_FILENAME
    index: dict[str, str] = {}
    if index_path.is_file():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RecordTranscriptError(
                f"{index_path} is not valid JSON ({exc.msg}); fix or remove it first"
            ) from exc
        if not isinstance(raw, dict):
            raise RecordTranscriptError(f"{index_path} must contain a JSON object")
        index = {str(key): str(value) for key, value in raw.items()}

    # Re-recording the same id replaces every older mapping to it.
    index = {key: value for key, value in index.items() if value != transcript_id}
    index[document] = transcript_id
    index_path.write_text(
        json.dumps(dict(sorted(index.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return index_path


def write_transcript(document: dict[str, Any], directory: Path) -> Path:
    """Validate, then write one transcript file atomically."""
    transcript_id = str(document.get("id", ""))
    try:
        validate_transcript(document, transcript_id=transcript_id)
    except TranscriptError as exc:
        raise RecordTranscriptError(f"the captured transcript failed validation: {exc}") from exc

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{transcript_id}.json"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record one real chat completion into a replay transcript. "
            "Costs exactly one provider request."
        ),
    )
    parser.add_argument(
        "--document", required=True, help="document filename, e.g. refund-policy.md"
    )
    parser.add_argument("--question", required=True, help="the question to ask the corpus")
    parser.add_argument(
        "--id", required=True, dest="transcript_id", help="transcript id / file stem"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API base URL ending in /api/v1 (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"name of the environment variable holding the key (default: {DEFAULT_API_KEY_ENV})",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=_env_int("DOCMIND_DEMO_TOP_K", 4),
        help="top_k sent with the question (default: 4)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="HTTP timeout in seconds (default: 300)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m demo.record_transcript``."""
    args = _build_parser().parse_args(argv)

    if not _ID_RE.match(args.transcript_id):
        print(
            f"error: --id '{args.transcript_id}' must match {_ID_RE.pattern}",
            file=sys.stderr,
        )
        return 2

    api_key = _read_api_key(args.api_key_env)
    if api_key is None:
        print(MISSING_KEY_MESSAGE, file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    try:
        frames, timings = capture(
            base_url=base_url,
            question=args.question,
            document=args.document,
            top_k=args.top_k,
            api_key=api_key,
            timeout=args.timeout,
        )
        chunk_count = _fetch_chunk_count(
            base_url, args.document, _auth_headers(api_key), args.timeout
        )
        transcript = build_transcript(
            transcript_id=args.transcript_id,
            question=args.question,
            document=args.document,
            frames=frames,
            timings_ms=timings,
            chunk_count=chunk_count,
            top_k=args.top_k,
        )
        path = write_transcript(transcript, args.out)
        index_path = update_index(
            args.out, document=args.document, transcript_id=args.transcript_id
        )
    except RecordTranscriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: could not write the transcript ({exc})", file=sys.stderr)
        return 1

    print(f"wrote {path}")
    print(f"updated {index_path}")
    print(_REMINDER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
