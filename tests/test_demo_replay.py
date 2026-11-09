"""Unit tests for the transcript replay contract (no network, no model)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from demo import replay
from demo.replay import (
    TranscriptError,
    available_transcripts,
    find_transcript,
    frame_delays,
    iter_replay_frames,
    load_transcript,
    load_transcript_index,
    resolve_chat_mode,
    validate_transcript,
)

TRANSCRIPT_ID = "refund-window"
DOCUMENT = "refund-policy.md"
TOKENS = ("Refunds ", "take five business days.")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_transcript() -> dict[str, Any]:
    """A minimal transcript that satisfies every rule."""
    return {
        "schema_version": 1,
        "kind": "transcript",
        "id": TRANSCRIPT_ID,
        "recorded_at": "2026-11-09T12:00:00+00:00",
        "question": "How long does a refund take?",
        "provenance": {
            "git_commit": "c6255eb",
            "source": "live capture: POST /api/v1/chat/completions",
            "llm_provider": "openai",
            "llm_model": "gpt-test",
            "embedding_model": "text-embedding-3-small",
            "embedding_dim": 1536,
            "top_k": 4,
        },
        "document": {"filename": DOCUMENT, "chunk_count": 3},
        "frames": [
            {
                "t_ms": 0,
                "type": "sources",
                "content": [
                    {
                        "filename": DOCUMENT,
                        "chunk_index": 0,
                        "score": 0.87,
                        "excerpt": "Refunds are processed within five business days.",
                    }
                ],
            },
            {"t_ms": 200, "type": "token", "content": TOKENS[0]},
            {"t_ms": 420, "type": "token", "content": TOKENS[1]},
            {"t_ms": 500, "type": "done"},
        ],
        "answer_sha256": _sha256("".join(TOKENS)),
        "timings_ms": {"retrieval": 0, "first_token": 200, "stream": 220, "frames": 500},
        "notes": [],
    }


def _validate(raw: dict[str, Any]) -> replay.Transcript:
    return validate_transcript(raw, transcript_id=TRANSCRIPT_ID)


def _write_transcript(directory: Path, raw: dict[str, Any], *, stem: str = TRANSCRIPT_ID) -> Path:
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _write_index(directory: Path, mapping: dict[str, str]) -> None:
    (directory / "index.json").write_text(json.dumps(mapping), encoding="utf-8")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
def test_valid_transcript_parses_frames_and_answer() -> None:
    transcript = _validate(valid_transcript())

    assert transcript.id == TRANSCRIPT_ID
    assert transcript.answer_text == "".join(TOKENS)
    assert transcript.sources[0].filename == DOCUMENT
    assert transcript.document_filename == DOCUMENT
    assert transcript.llm_model == "gpt-test"
    assert transcript.recorded_date == "2026-11-09"


def test_unknown_schema_version_is_rejected() -> None:
    raw = valid_transcript()
    raw["schema_version"] = 2

    with pytest.raises(TranscriptError, match="unsupported schema_version 2"):
        _validate(raw)


def test_wrong_kind_is_rejected() -> None:
    raw = valid_transcript()
    raw["kind"] = "answer"

    with pytest.raises(TranscriptError, match="expected kind 'transcript'"):
        _validate(raw)


def test_unknown_frame_type_is_rejected() -> None:
    raw = valid_transcript()
    raw["frames"][1]["type"] = "audio"

    with pytest.raises(TranscriptError, match="unknown type 'audio'"):
        _validate(raw)


def test_non_monotonic_t_ms_is_rejected() -> None:
    raw = valid_transcript()
    raw["frames"][2]["t_ms"] = 100  # before the 200 ms token

    with pytest.raises(TranscriptError, match="t_ms must be non-decreasing"):
        _validate(raw)


def test_last_frame_must_be_done() -> None:
    raw = valid_transcript()
    raw["frames"] = raw["frames"][:-1]

    with pytest.raises(TranscriptError, match="the last frame must be 'done'"):
        _validate(raw)


def test_empty_frames_are_rejected() -> None:
    raw = valid_transcript()
    raw["frames"] = []

    with pytest.raises(TranscriptError, match="'frames' must not be empty"):
        _validate(raw)


def test_answer_sha256_mismatch_is_rejected() -> None:
    raw = valid_transcript()
    raw["answer_sha256"] = _sha256("something else")

    with pytest.raises(TranscriptError, match="answer_sha256 does not match"):
        _validate(raw)


def test_single_error_transcript_is_allowed() -> None:
    raw = valid_transcript()
    raw["frames"] = [{"t_ms": 0, "type": "error", "message": "provider unavailable"}]
    raw["answer_sha256"] = _sha256("")

    transcript = _validate(raw)

    assert transcript.frames[0].type == "error"
    assert transcript.frames[0].message == "provider unavailable"


def test_declared_id_must_match_file_name() -> None:
    with pytest.raises(TranscriptError, match="does not match the file name"):
        validate_transcript(valid_transcript(), transcript_id="other-id")


# ---------------------------------------------------------------------------
# Credential and path hygiene
# ---------------------------------------------------------------------------
def test_api_key_field_is_rejected() -> None:
    raw = valid_transcript()
    raw["provenance"]["api_key"] = "redacted"

    with pytest.raises(TranscriptError, match="forbidden credential field"):
        _validate(raw)


def test_sk_prefixed_value_is_rejected() -> None:
    raw = valid_transcript()
    raw["notes"] = ["sk-not-a-real-key"]

    with pytest.raises(TranscriptError, match="credential-shaped value"):
        _validate(raw)


def test_absolute_path_is_rejected() -> None:
    raw = valid_transcript()
    raw["frames"][0]["content"][0]["excerpt"] = "/home/operator/refund-policy.md"

    with pytest.raises(TranscriptError, match="absolute path"):
        _validate(raw)


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------
def test_frame_delays_respect_speed_and_cap() -> None:
    frames = _validate(valid_transcript()).frames

    scaled = frame_delays(frames, speed=2.0, max_frame_ms=300.0)
    capped = frame_delays(frames, speed=0.5, max_frame_ms=300.0)

    assert scaled[0] == 0.0
    assert scaled[1] == pytest.approx(100.0)  # 200 ms / 2
    assert all(0.0 <= delay <= 300.0 for delay in capped)
    assert capped[1] == pytest.approx(300.0)  # 400 ms capped at 300


def test_iter_replay_frames_respects_the_total_budget() -> None:
    transcript = _validate(valid_transcript())

    pairs = list(
        iter_replay_frames(
            transcript,
            speed=1.0,
            max_frame_ms=10_000.0,
            max_total_seconds=0.05,
        )
    )

    assert len(pairs) == len(transcript.frames)
    assert sum(delay for _, delay in pairs) <= 0.05 + 1e-9


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_missing_directory_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOCMIND_TRANSCRIPTS_DIR", str(tmp_path / "absent"))

    assert load_transcript_index() == {}
    assert find_transcript(DOCUMENT) is None


def test_find_transcript_uses_the_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOCMIND_TRANSCRIPTS_DIR", str(tmp_path))
    _write_transcript(tmp_path, valid_transcript())
    _write_index(tmp_path, {DOCUMENT: TRANSCRIPT_ID})

    transcript = find_transcript(DOCUMENT)

    assert transcript is not None
    assert transcript.id == TRANSCRIPT_ID
    assert find_transcript("other.md") is None
    assert [item.id for item in available_transcripts()] == [TRANSCRIPT_ID]


def test_load_transcript_reports_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(TranscriptError, match="not valid JSON"):
        load_transcript(path)


def test_malformed_index_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOCMIND_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / "index.json").write_text("[]", encoding="utf-8")

    with pytest.raises(TranscriptError, match="must be an object"):
        load_transcript_index()


# ---------------------------------------------------------------------------
# Chat-mode configuration
# ---------------------------------------------------------------------------
def test_chat_mode_defaults_to_replay_for_the_public_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCMIND_DEMO_MODE", "1")
    monkeypatch.delenv("DOCMIND_CHAT_MODE", raising=False)

    assert resolve_chat_mode(None, demo_mode=True) == "replay"


def test_chat_mode_defaults_to_live_locally() -> None:
    assert resolve_chat_mode(None, demo_mode=False) == "live"
    assert resolve_chat_mode("", demo_mode=False) == "live"


def test_explicit_chat_mode_is_honoured() -> None:
    assert resolve_chat_mode("replay", demo_mode=False) == "replay"
    assert resolve_chat_mode("LIVE", demo_mode=True) == "live"


def test_unknown_chat_mode_fails_loudly() -> None:
    with pytest.raises(ValueError, match="DOCMIND_CHAT_MODE must be one of"):
        resolve_chat_mode("expensive", demo_mode=True)
