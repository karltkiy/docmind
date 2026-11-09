"""Replay recorded chat transcripts without touching a model provider.

The public demo must be able to answer questions without spending tokens. This
module is the pure, Streamlit-free half of that story: it defines the transcript
format, validates it strictly, and turns the recorded frames back into the same
pacing the original stream had.

The transcript format mirrors the SSE contract already emitted by ``POST
/api/v1/chat/completions`` (see :mod:`src.schemas.chat`):

.. code-block:: json

    {
      "schema_version": 1,
      "kind": "transcript",
      "id": "refund-window",
      "recorded_at": "2026-11-09T12:00:00+00:00",
      "question": "How long does a refund take?",
      "provenance": {
        "git_commit": "...",
        "source": "live capture: POST /api/v1/chat/completions",
        "llm_provider": "openai",
        "llm_model": "gpt-...",
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
        "top_k": 4
      },
      "document": {"filename": "refund-policy.md", "chunk_count": 12},
      "frames": [
        {"t_ms": 0, "type": "sources", "content": [...]},
        {"t_ms": 420, "type": "token", "content": "Refunds "},
        {"t_ms": 640, "type": "done"}
      ],
      "answer_sha256": "...",
      "timings_ms": {"retrieval": 380, "first_token": 420, "stream": 220, "frames": 640},
      "notes": []
    }

The module never invents an answer: a missing transcript is reported as such,
and there is deliberately no fallback to a live call here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Transcript schema version this build understands.
SCHEMA_VERSION = 1

#: Value of the ``kind`` field in a transcript file.
TRANSCRIPT_KIND = "transcript"

#: Frame types allowed by the replay contract.
FRAME_TYPES: frozenset[str] = frozenset({"sources", "token", "done", "error"})

#: Object keys that would indicate a credential was written to a transcript.
FORBIDDEN_KEYS: frozenset[str] = frozenset({"api_key", "authorization", "openai_api_key"})

#: String prefixes that look like provider secrets.
FORBIDDEN_VALUE_PREFIXES: tuple[str, ...] = ("sk-",)

#: Substrings that indicate an absolute path leaked into a transcript.
ABSOLUTE_PATH_MARKERS: tuple[str, ...] = ("c:\\", "/home/", "/opt/", "/users/")

#: Windows drive-letter absolute path (``C:\...`` / ``C:/...``).
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[a-zA-Z]:[\\/]")

#: Transcripts live beside the other committed demo fixtures.
TRANSCRIPTS_DIR = Path(__file__).resolve().parent / "fixtures" / "transcripts"

#: File name of the ``document filename -> transcript id`` index.
INDEX_FILENAME = "index.json"

#: Default replay pacing.
DEFAULT_SPEED = 8.0
DEFAULT_MAX_FRAME_MS = 150.0
DEFAULT_MAX_TOTAL_SECONDS = 8.0

#: Environment variables understood by :mod:`demo.app_ui` and this module.
SPEED_ENV_VAR = "DOCMIND_REPLAY_SPEED"
MAX_FRAME_ENV_VAR = "DOCMIND_REPLAY_MAX_FRAME_MS"
MAX_SECONDS_ENV_VAR = "DOCMIND_REPLAY_MAX_SECONDS"
TRANSCRIPTS_DIR_ENV_VAR = "DOCMIND_TRANSCRIPTS_DIR"

#: Chat modes accepted by ``DOCMIND_CHAT_MODE``.
CHAT_MODES: frozenset[str] = frozenset({"replay", "live"})


class TranscriptError(RuntimeError):
    """Raised when a transcript is missing, unreadable or violates the contract.

    The message always names the transcript id (or file) and the reason, so the
    UI can show something actionable instead of a stack trace.
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TranscriptSource:
    """One retrieved chunk cited by a recorded answer."""

    filename: str
    chunk_index: int
    score: float
    excerpt: str

    def as_dict(self) -> dict[str, Any]:
        """Return the shape the live UI expects from an SSE ``sources`` frame."""
        return {
            "filename": self.filename,
            "chunk_index": self.chunk_index,
            "score": self.score,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class TranscriptFrame:
    """A single recorded SSE frame."""

    t_ms: int
    type: str
    content: str | None = None
    sources: tuple[TranscriptSource, ...] = ()
    message: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """Return the frame as the SSE-style dictionary the UI consumes."""
        if self.type == "token":
            return {"type": "token", "content": self.content or ""}
        if self.type == "sources":
            return {"type": "sources", "content": [source.as_dict() for source in self.sources]}
        if self.type == "error":
            return {"type": "error", "message": self.message or ""}
        return {"type": "done"}


@dataclass(frozen=True)
class Transcript:
    """A validated recorded answer."""

    id: str
    schema_version: int
    recorded_at: datetime
    question: str
    provenance: dict[str, Any]
    document: dict[str, Any]
    frames: tuple[TranscriptFrame, ...]
    answer_sha256: str
    timings_ms: dict[str, float]
    notes: tuple[str, ...]
    path: Path | None = field(default=None, repr=False)

    @property
    def answer_text(self) -> str:
        """Concatenation of every token frame, in order."""
        return "".join(frame.content or "" for frame in self.frames if frame.type == "token")

    @property
    def sources(self) -> tuple[TranscriptSource, ...]:
        """The most recent ``sources`` frame, or an empty tuple."""
        for frame in self.frames:
            if frame.type == "sources":
                return frame.sources
        return ()

    @property
    def document_filename(self) -> str:
        """Filename the transcript was recorded against."""
        return str(self.document.get("filename", ""))

    @property
    def llm_model(self) -> str:
        """Model recorded in the provenance block (``unknown`` when not known)."""
        return str(self.provenance.get("llm_model", "unknown"))

    @property
    def recorded_date(self) -> str:
        """``YYYY-MM-DD`` of the recording, for UI captions."""
        return self.recorded_at.date().isoformat()


# ---------------------------------------------------------------------------
# Small validation helpers (clear messages, no guessing)
# ---------------------------------------------------------------------------
def _fail(transcript_id: str, reason: str) -> TranscriptError:
    return TranscriptError(f"transcript '{transcript_id}': {reason}")


def _require_object(value: Any, name: str, transcript_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(transcript_id, f"field '{name}' must be a JSON object")
    return value


def _require_str(value: Any, name: str, transcript_id: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(transcript_id, f"field '{name}' must be a string")
    if not allow_empty and not value.strip():
        raise _fail(transcript_id, f"field '{name}' must not be empty")
    return value


def _require_int(value: Any, name: str, transcript_id: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(transcript_id, f"field '{name}' must be an integer")
    if value < minimum:
        raise _fail(transcript_id, f"field '{name}' must be >= {minimum}")
    return value


def _require_number(value: Any, name: str, transcript_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(transcript_id, f"field '{name}' must be a number")
    return float(value)


def _require_list(value: Any, name: str, transcript_id: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(transcript_id, f"field '{name}' must be a list")
    return value


def _parse_timestamp(value: Any, name: str, transcript_id: str) -> datetime:
    text = _require_str(value, name, transcript_id)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise _fail(transcript_id, f"field '{name}' is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _scan_for_forbidden(value: Any, transcript_id: str, trail: str = "") -> None:
    """Reject credential-shaped keys/values and absolute paths anywhere."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in FORBIDDEN_KEYS:
                raise _fail(transcript_id, f"forbidden credential field '{trail}{key_text}'")
            _scan_for_forbidden(item, transcript_id, f"{trail}{key_text}.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_for_forbidden(item, transcript_id, f"{trail}[{index}].")
        return
    if isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith(FORBIDDEN_VALUE_PREFIXES):
            location = trail.rstrip(".")
            raise _fail(transcript_id, f"forbidden credential-shaped value at '{location}'")
        if _WINDOWS_ABSOLUTE_RE.match(value) or any(
            marker in lowered for marker in ABSOLUTE_PATH_MARKERS
        ):
            raise _fail(transcript_id, f"absolute path is not allowed at '{trail.rstrip('.')}'")


def _parse_source(raw: Any, index: int, transcript_id: str) -> TranscriptSource:
    where = f"frames[{index}].content"
    source = _require_object(raw, where, transcript_id)
    return TranscriptSource(
        filename=_require_str(source.get("filename"), f"{where}.filename", transcript_id),
        chunk_index=_require_int(source.get("chunk_index"), f"{where}.chunk_index", transcript_id),
        score=_require_number(source.get("score"), f"{where}.score", transcript_id),
        excerpt=_require_str(
            source.get("excerpt"), f"{where}.excerpt", transcript_id, allow_empty=True
        ),
    )


def _parse_frame(raw: Any, index: int, transcript_id: str) -> TranscriptFrame:
    where = f"frames[{index}]"
    frame = _require_object(raw, where, transcript_id)
    t_ms = _require_int(frame.get("t_ms"), f"{where}.t_ms", transcript_id)
    frame_type = _require_str(frame.get("type"), f"{where}.type", transcript_id)
    if frame_type not in FRAME_TYPES:
        known = ", ".join(sorted(FRAME_TYPES))
        raise _fail(
            transcript_id, f"frame {index} has unknown type '{frame_type}' (known: {known})"
        )

    if frame_type == "token":
        content = _require_str(
            frame.get("content"), f"{where}.content", transcript_id, allow_empty=True
        )
        return TranscriptFrame(t_ms=t_ms, type=frame_type, content=content)
    if frame_type == "error":
        message = _require_str(frame.get("message"), f"{where}.message", transcript_id)
        return TranscriptFrame(t_ms=t_ms, type=frame_type, message=message)
    if frame_type == "sources":
        raw_sources = frame.get("content", frame.get("sources"))
        items = _require_list(raw_sources, f"{where}.content", transcript_id)
        sources = tuple(
            _parse_source(item, index, transcript_id) for index, item in enumerate(items)
        )
        return TranscriptFrame(t_ms=t_ms, type=frame_type, sources=sources)
    return TranscriptFrame(t_ms=t_ms, type="done")


def _parse_provenance(raw: Any, transcript_id: str) -> dict[str, Any]:
    provenance = _require_object(raw, "provenance", transcript_id)
    for key in ("git_commit", "source", "llm_provider", "llm_model", "embedding_model"):
        _require_str(provenance.get(key), f"provenance.{key}", transcript_id)
    _require_int(provenance.get("embedding_dim"), "provenance.embedding_dim", transcript_id)
    _require_int(provenance.get("top_k"), "provenance.top_k", transcript_id, minimum=1)
    return dict(provenance)


# ---------------------------------------------------------------------------
# Public validation / loading API
# ---------------------------------------------------------------------------
def validate_transcript(
    raw: Any,
    *,
    transcript_id: str | None = None,
    path: Path | None = None,
) -> Transcript:
    """Validate a decoded transcript and return the typed object.

    Args:
        raw: Decoded JSON document.
        transcript_id: Expected id (usually the file stem). When given, the
            declared ``id`` must match it.
        path: Source path, kept on the result for error reporting.

    Raises:
        TranscriptError: With a message naming the transcript and the reason.
    """
    if not isinstance(raw, dict):
        raise TranscriptError("transcript '<unknown>': top-level JSON value must be an object")

    declared_id = raw.get("id")
    identifier = transcript_id or (
        declared_id if isinstance(declared_id, str) and declared_id.strip() else "<unknown>"
    )

    schema_version = _require_int(raw.get("schema_version"), "schema_version", identifier)
    if schema_version != SCHEMA_VERSION:
        raise _fail(
            identifier,
            f"unsupported schema_version {schema_version} (supported: {SCHEMA_VERSION})",
        )

    kind = _require_str(raw.get("kind"), "kind", identifier)
    if kind != TRANSCRIPT_KIND:
        raise _fail(identifier, f"expected kind '{TRANSCRIPT_KIND}', found '{kind}'")

    declared = _require_str(declared_id, "id", identifier)
    if transcript_id is not None and declared != transcript_id:
        raise _fail(
            transcript_id,
            f"declared id '{declared}' does not match the file name '{transcript_id}'",
        )

    recorded_at = _parse_timestamp(raw.get("recorded_at"), "recorded_at", identifier)
    question = _require_str(raw.get("question"), "question", identifier)
    provenance = _parse_provenance(raw.get("provenance"), identifier)

    document = _require_object(raw.get("document"), "document", identifier)
    _require_str(document.get("filename"), "document.filename", identifier)
    _require_int(document.get("chunk_count"), "document.chunk_count", identifier)

    raw_frames = _require_list(raw.get("frames"), "frames", identifier)
    if not raw_frames:
        raise _fail(identifier, "field 'frames' must not be empty")

    frames: list[TranscriptFrame] = []
    previous_t_ms = -1
    for index, item in enumerate(raw_frames):
        frame = _parse_frame(item, index, identifier)
        if frame.t_ms < previous_t_ms:
            raise _fail(
                identifier,
                f"frame {index} has t_ms={frame.t_ms}, before the previous frame "
                f"({previous_t_ms}); t_ms must be non-decreasing",
            )
        previous_t_ms = frame.t_ms
        frames.append(frame)

    last = frames[-1]
    if not (last.type == "done" or (len(frames) == 1 and last.type == "error")):
        raise _fail(identifier, "the last frame must be 'done' (or a single 'error' frame)")

    answer_sha256 = _require_str(raw.get("answer_sha256"), "answer_sha256", identifier)
    digest = hashlib.sha256(
        "".join(frame.content or "" for frame in frames if frame.type == "token").encode("utf-8")
    ).hexdigest()
    if digest != answer_sha256.lower():
        raise _fail(
            identifier,
            "answer_sha256 does not match the concatenated token content "
            f"(expected {answer_sha256}, computed {digest})",
        )

    timings_ms: dict[str, float] = {}
    raw_timings = raw.get("timings_ms")
    if raw_timings is not None:
        timings_object = _require_object(raw_timings, "timings_ms", identifier)
        for key in ("retrieval", "first_token", "stream", "frames"):
            if key not in timings_object:
                continue
            value = _require_number(timings_object[key], f"timings_ms.{key}", identifier)
            if value < 0:
                raise _fail(identifier, f"field 'timings_ms.{key}' must be >= 0")
            timings_ms[key] = value

    notes_raw = raw.get("notes", [])
    notes_items = _require_list(notes_raw, "notes", identifier)
    notes = tuple(
        _require_str(item, f"notes[{index}]", identifier) for index, item in enumerate(notes_items)
    )

    _scan_for_forbidden(raw, identifier)

    return Transcript(
        id=declared,
        schema_version=schema_version,
        recorded_at=recorded_at,
        question=question,
        provenance=provenance,
        document=dict(document),
        frames=tuple(frames),
        answer_sha256=answer_sha256.lower(),
        timings_ms=timings_ms,
        notes=notes,
        path=path,
    )


def load_transcript(path: Path | str) -> Transcript:
    """Read and validate a transcript file.

    Raises:
        TranscriptError: The file is missing, not JSON, or fails validation.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise TranscriptError(f"transcript '{file_path.name}': file not found at {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranscriptError(
            f"transcript '{file_path.stem}': not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc
    except OSError as exc:
        raise TranscriptError(
            f"transcript '{file_path.stem}': could not be read: {exc.strerror}"
        ) from exc
    return validate_transcript(raw, transcript_id=file_path.stem, path=file_path)


# ---------------------------------------------------------------------------
# Directory / index discovery
# ---------------------------------------------------------------------------
def transcripts_directory() -> Path:
    """Directory transcripts are read from (overridable for deployments/tests)."""
    override = os.getenv(TRANSCRIPTS_DIR_ENV_VAR, "").strip()
    return Path(override) if override else TRANSCRIPTS_DIR


def load_transcript_index(directory: Path | None = None) -> dict[str, str]:
    """Read ``index.json`` mapping *document filename* to *transcript id*.

    A missing directory or file is not an error: it means no recordings are
    shipped, and the UI is expected to say so. A malformed index is an error.
    """
    index_path = (directory or transcripts_directory()) / INDEX_FILENAME
    if not index_path.is_file():
        return {}
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranscriptError(
            f"transcript index '{INDEX_FILENAME}': not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc
    except OSError as exc:
        raise TranscriptError(
            f"transcript index '{INDEX_FILENAME}': could not be read: {exc.strerror}"
        ) from exc
    if not isinstance(raw, dict):
        raise TranscriptError(
            f"transcript index '{INDEX_FILENAME}': top-level JSON value must be an object"
        )
    index: dict[str, str] = {}
    for filename, transcript_id in raw.items():
        if not isinstance(filename, str) or not filename.strip():
            raise TranscriptError(
                f"transcript index '{INDEX_FILENAME}': keys must be non-empty strings"
            )
        if not isinstance(transcript_id, str) or not transcript_id.strip():
            raise TranscriptError(
                f"transcript index '{INDEX_FILENAME}': value for '{filename}' "
                "must be a non-empty transcript id"
            )
        index[filename] = transcript_id
    return index


def find_transcript(document_filename: str) -> Transcript | None:
    """Return the transcript recorded for ``document_filename``, or ``None``.

    This function never calls a model and never falls back to a live request: a
    missing entry is a legitimate "not recorded" answer for the UI.
    """
    transcript_id = load_transcript_index().get(document_filename)
    if not transcript_id:
        return None
    path = transcripts_directory() / f"{transcript_id}.json"
    if not path.is_file():
        return None
    return load_transcript(path)


def available_transcripts(directory: Path | None = None) -> list[Transcript]:
    """Return every index entry that loads successfully, sorted by id.

    Broken records are skipped: this is a discovery helper for the "nothing is
    recorded" message, not a validator. Use :func:`load_transcript` directly
    when a validation error should be surfaced.
    """
    directory = directory or transcripts_directory()
    identifiers = sorted(set(load_transcript_index(directory).values()))
    transcripts: list[Transcript] = []
    for transcript_id in identifiers:
        path = directory / f"{transcript_id}.json"
        if not path.is_file():
            continue
        try:
            transcripts.append(load_transcript(path))
        except TranscriptError:
            continue
    return transcripts


# ---------------------------------------------------------------------------
# Replay pacing
# ---------------------------------------------------------------------------
def frame_delays(
    frames: tuple[TranscriptFrame, ...] | list[TranscriptFrame],
    *,
    speed: float = DEFAULT_SPEED,
    max_frame_ms: float = DEFAULT_MAX_FRAME_MS,
) -> list[float]:
    """Return the pause, in milliseconds, to wait *before* each frame.

    Recorded ``t_ms`` deltas are divided by ``speed`` and capped at
    ``max_frame_ms`` so a slow recorded answer cannot stall the UI. The first
    frame has no delay.
    """
    effective_speed = speed if speed > 0 else DEFAULT_SPEED
    cap = max(0.0, float(max_frame_ms))
    delays: list[float] = [0.0]
    for previous, current in zip(frames, frames[1:], strict=False):
        delta = max(0, current.t_ms - previous.t_ms) / effective_speed
        delays.append(min(float(delta), cap))
    return delays


def iter_replay_frames(
    transcript: Transcript,
    *,
    speed: float = DEFAULT_SPEED,
    max_frame_ms: float = DEFAULT_MAX_FRAME_MS,
    max_total_seconds: float = DEFAULT_MAX_TOTAL_SECONDS,
) -> Iterator[tuple[TranscriptFrame, float]]:
    """Yield ``(frame, delay_seconds)`` pairs, bounded by ``max_total_seconds``.

    The generator does not sleep — the caller decides how to pause — but the
    sum of the yielded delays never exceeds the budget, so even a pathological
    transcript cannot make a page hang.
    """
    budget = max(0.0, float(max_total_seconds))
    elapsed = 0.0
    delays = frame_delays(transcript.frames, speed=speed, max_frame_ms=max_frame_ms)
    for frame, delay_ms in zip(transcript.frames, delays, strict=False):
        delay = delay_ms / 1000.0
        if elapsed + delay > budget:
            delay = max(0.0, budget - elapsed)
        elapsed += delay
        yield frame, delay


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _float_env(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def configured_speed() -> float:
    """Replay speed from ``DOCMIND_REPLAY_SPEED`` (default 8.0)."""
    return _float_env(SPEED_ENV_VAR, DEFAULT_SPEED, minimum=0.001)


def configured_max_frame_ms() -> float:
    """Per-frame pause cap from ``DOCMIND_REPLAY_MAX_FRAME_MS`` (default 150)."""
    return _float_env(MAX_FRAME_ENV_VAR, DEFAULT_MAX_FRAME_MS, minimum=0.0)


def configured_max_seconds() -> float:
    """Total replay budget from ``DOCMIND_REPLAY_MAX_SECONDS`` (default 8.0)."""
    return _float_env(MAX_SECONDS_ENV_VAR, DEFAULT_MAX_TOTAL_SECONDS, minimum=0.0)


def resolve_chat_mode(raw: str | None, *, demo_mode: bool) -> str:
    """Resolve ``DOCMIND_CHAT_MODE`` into ``"replay"`` or ``"live"``.

    Defaults to ``replay`` when the public demo mode is on (a public deployment
    must not spend tokens by accident) and to ``live`` otherwise. An explicit
    value outside the allowed set is a configuration error and fails loudly
    rather than silently choosing a mode the operator did not ask for.
    """
    if raw is None or not raw.strip():
        return "replay" if demo_mode else "live"
    mode = raw.strip().lower()
    if mode not in CHAT_MODES:
        known = ", ".join(sorted(CHAT_MODES))
        raise ValueError(f"DOCMIND_CHAT_MODE must be one of {{{known}}}; got {raw!r}")
    return mode
