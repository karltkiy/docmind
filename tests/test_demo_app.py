"""AppTest smoke tests for the demo UI.

Streamlit is a ``demo``-extra dependency, while CI installs only ``[dev]``, so
the whole module is skipped when it is not installed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("streamlit")

import requests  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parents[1] / "demo" / "app_ui.py")
CHAT_VIEW = "Chat & documents"
METRICS_VIEW = "Metrics & evidence"
CARD_LABELS = (
    "hit@k",
    "MRR",
    "Context recall",
    "Retrieval p95",
    "Ingestion median",
    "Tests collected",
    "Coverage (src/)",
    "API endpoints",
)


class _UnauthorizedResponse:
    """Minimal stand-in for a 401 response object."""

    status_code = 401

    def json(self) -> Any:  # pragma: no cover - never reached on 401
        raise ValueError("no body on 401")


class _DocumentsResponse:
    """Minimal stand-in for ``GET /documents``."""

    status_code = 200

    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._documents


DOCUMENTS = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "filename": "refund-policy.md",
        "chunk_count": 3,
    }
]
TRANSCRIPT_TOKENS = ("Refunds ", "take five business days.")


def _documents_get(*args: Any, **kwargs: Any) -> _DocumentsResponse:
    return _DocumentsResponse(DOCUMENTS)


def _write_transcript(directory: Path) -> None:
    """Write one valid recording plus its index into ``directory``."""
    answer = "".join(TRANSCRIPT_TOKENS)
    document = {
        "schema_version": 1,
        "kind": "transcript",
        "id": "refund-window",
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
        "document": {"filename": "refund-policy.md", "chunk_count": 3},
        "frames": [
            {
                "t_ms": 0,
                "type": "sources",
                "content": [
                    {
                        "filename": "refund-policy.md",
                        "chunk_index": 0,
                        "score": 0.87,
                        "excerpt": "Refunds are processed within five business days.",
                    }
                ],
            },
            {"t_ms": 200, "type": "token", "content": TRANSCRIPT_TOKENS[0]},
            {"t_ms": 420, "type": "token", "content": TRANSCRIPT_TOKENS[1]},
            {"t_ms": 500, "type": "done"},
        ],
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "timings_ms": {"retrieval": 0, "first_token": 200, "stream": 220, "frames": 500},
        "notes": [],
    }
    (directory / "refund-window.json").write_text(json.dumps(document), encoding="utf-8")
    (directory / "index.json").write_text(
        json.dumps({"refund-policy.md": "refund-window"}), encoding="utf-8"
    )


def _configure_demo_chat(
    monkeypatch: pytest.MonkeyPatch,
    transcripts: Path,
    *,
    chat_mode: str | None = "replay",
) -> None:
    """Point the app at an empty/recorded transcript directory and forbid POSTs."""
    monkeypatch.setenv("DOCMIND_DEMO_MODE", "1")
    if chat_mode is None:
        monkeypatch.delenv("DOCMIND_CHAT_MODE", raising=False)
    else:
        monkeypatch.setenv("DOCMIND_CHAT_MODE", chat_mode)
    monkeypatch.setenv("DOCMIND_TRANSCRIPTS_DIR", str(transcripts))
    monkeypatch.setenv("DOCMIND_REPLAY_MAX_SECONDS", "0")
    monkeypatch.setenv("DOCMIND_REPLAY_MAX_FRAME_MS", "0")
    monkeypatch.setattr(requests, "get", _documents_get)
    monkeypatch.setattr(requests, "post", _forbidden_post)


def _element_text(*groups: Any) -> str:
    return " ".join(str(getattr(element, "value", "")) for group in groups for element in group)


def _connection_error(*args: Any, **kwargs: Any) -> None:
    raise requests.ConnectionError("metrics API is unreachable")


def _forbidden_post(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("the metrics view must not POST to /chat/completions")


@pytest.fixture(autouse=True)
def _reset_streamlit_caches() -> Iterator[None]:
    """Streamlit caches survive between AppTest runs; clear them per test."""
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _run(view: str | None = None) -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=60)
    if view is not None:
        app.session_state["nav"] = view
    app.run()
    return app


def test_app_renders_without_exceptions() -> None:
    app = _run()

    assert list(app.exception) == []
    options = list(app.sidebar.radio[0].options)
    assert CHAT_VIEW in options
    assert METRICS_VIEW in options


def test_metrics_view_renders_the_headline_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "get", _connection_error)

    app = _run(METRICS_VIEW)

    assert list(app.exception) == []
    labels = [metric.label for metric in app.metric]
    for expected in CARD_LABELS:
        assert expected in labels


def test_unreachable_api_warns_but_does_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "get", _connection_error)

    app = _run(METRICS_VIEW)

    assert list(app.exception) == []
    assert app.warning
    assert not app.error


def test_unauthorized_snapshot_points_at_the_api_key_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _UnauthorizedResponse())
    monkeypatch.setenv("DOCMIND_API_KEY", "unit-test-key-value")

    app = _run(METRICS_VIEW)

    assert list(app.exception) == []
    warnings = " ".join(str(item.value) for item in app.warning)
    assert "DOCMIND_API_KEY" in warnings
    assert "unit-test-key-value" not in warnings
    assert not app.error


def test_metrics_view_never_posts_to_the_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "get", _connection_error)
    monkeypatch.setattr(requests, "post", _forbidden_post)

    app = _run(METRICS_VIEW)

    assert list(app.exception) == []
    assert not app.error


def test_missing_fixtures_render_an_error_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCMIND_FIXTURES_DIR", str(tmp_path))
    monkeypatch.setattr(requests, "get", _connection_error)

    app = _run(METRICS_VIEW)

    assert list(app.exception) == []
    assert app.error


def test_metrics_selection_is_kept_in_the_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "get", _connection_error)

    app = _run(METRICS_VIEW)

    assert app.query_params.get("view") == "metrics"


def test_replay_mode_renders_the_recording_without_posting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_transcript(tmp_path)
    _configure_demo_chat(monkeypatch, tmp_path)

    app = _run(CHAT_VIEW)
    assert list(app.exception) == []
    assert app.chat_input, "replay mode with a recording should render a chat input"

    app.chat_input[0].set_value("How long does a refund take?").run()

    assert list(app.exception) == []
    assert "Recorded answer" in _element_text(app.info)
    assert "replay" in _element_text(app.caption).lower()
    assert "recorded" in _element_text(app.caption).lower()


def test_replay_mode_without_a_transcript_never_falls_back_to_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_demo_chat(monkeypatch, tmp_path)

    app = _run(CHAT_VIEW)

    assert list(app.exception) == []
    text = _element_text(
        app.warning,
        app.info,
        app.caption,
        app.markdown,
        getattr(app, "code", []),
    )
    assert "No recorded transcript" in text
    assert "python -m demo.record_transcript" in text
    assert not app.chat_input


def test_demo_mode_without_chat_mode_defaults_to_replay_in_the_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_demo_chat(monkeypatch, tmp_path, chat_mode=None)

    app = _run(CHAT_VIEW)

    assert list(app.exception) == []
    warnings = _element_text(app.warning)
    assert "No recorded transcript" in warnings
    assert "Live mode: every question spends provider tokens." not in warnings


def test_live_mode_shows_the_token_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_DEMO_MODE", "0")
    monkeypatch.setenv("DOCMIND_CHAT_MODE", "live")
    monkeypatch.setattr(requests, "get", _documents_get)

    app = _run(CHAT_VIEW)

    assert list(app.exception) == []
    assert "Live mode: every question spends provider tokens." in _element_text(app.warning)
