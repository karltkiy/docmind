"""Guards for the OpenAI chat-model migration (GPT-6 Luna + reasoning effort).

The provider documents the effort levels ``none | low | medium | high | xhigh |
max`` and states that Chat Completions takes the *flat* ``reasoning_effort``
argument, while the nested ``reasoning.effort`` object belongs to the Responses
API. Both facts are asserted here so a future refactor cannot silently switch the
shape or drop the parameter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from src.config import _DEFAULT_LLM_MODELS, Settings, settings
from src.services.rag_engine import RAGEngine

_TEST_DSN = "postgresql+asyncpg://postgres:postgres@localhost:5432/docmind_test"


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
def test_default_openai_model_is_the_documented_release() -> None:
    """The shipped default must be the model the project claims to use."""
    assert _DEFAULT_LLM_MODELS["openai"] == "gpt-6-luna"
    assert settings.llm_model == "gpt-6-luna"


def test_reasoning_effort_defaults_to_none() -> None:
    """``none`` is a deliberate deviation from the provider default (medium)."""
    assert settings.LLM_REASONING_EFFORT == "none"


def test_explicit_model_override_still_wins() -> None:
    """``LLM_MODEL`` must keep overriding the per-provider default."""
    configured = Settings(DATABASE_URL=_TEST_DSN, LLM_MODEL="gpt-6-luna-snapshot")
    assert configured.llm_model == "gpt-6-luna-snapshot"


@pytest.mark.parametrize("value", ["none", "low", "medium", "high", "xhigh", "max"])
def test_documented_effort_levels_are_accepted(value: str) -> None:
    configured = Settings(DATABASE_URL=_TEST_DSN, LLM_REASONING_EFFORT=value)
    assert configured.LLM_REASONING_EFFORT == value


def test_undocumented_effort_level_fails_fast() -> None:
    """A typo must break startup, not every individual chat request."""
    with pytest.raises(ValidationError):
        Settings(DATABASE_URL=_TEST_DSN, LLM_REASONING_EFFORT="extreme")


# ---------------------------------------------------------------------------
# Request wiring
# ---------------------------------------------------------------------------
async def _stream_chunks(contents: list[str]) -> AsyncIterator[Any]:
    for content in contents:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


class _FakeCompletions:
    """Records the keyword arguments each chat-completion call was made with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _stream_chunks(["Hello", " world"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))])


class _FakeOpenAIClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _engine_with_fake_openai() -> tuple[RAGEngine, _FakeCompletions]:
    """Build an engine whose OpenAI transport is a recording stub."""
    engine = RAGEngine()
    engine.llm_provider = "openai"
    completions = _FakeCompletions()
    engine._openai = _FakeOpenAIClient(completions)  # type: ignore[assignment]
    return engine, completions


async def test_generate_answer_sends_flat_reasoning_effort() -> None:
    engine, completions = _engine_with_fake_openai()

    answer = await engine.generate_answer("question")

    assert answer == "answer"
    call = completions.calls[0]
    assert call["model"] == settings.llm_model
    assert call["reasoning_effort"] == "none"
    # The nested Responses-API object must never be sent to chat.completions.
    assert "reasoning" not in call


async def test_generate_answer_stream_sends_flat_reasoning_effort() -> None:
    engine, completions = _engine_with_fake_openai()

    tokens = [token async for token in engine.generate_answer_stream("question")]

    assert tokens == ["Hello", " world"]
    call = completions.calls[0]
    assert call["stream"] is True
    assert call["reasoning_effort"] == "none"
    assert "reasoning" not in call


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        yield json.dumps({"response": "local answer", "done": True})


class _FakeHTTPStream:
    def __init__(self, response: _FakeHTTPResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeHTTPResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeHTTPClient:
    """Captures the JSON payloads sent to the Ollama generate endpoint."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, json: dict[str, Any]) -> _FakeHTTPStream:
        self.payloads.append(json)
        return _FakeHTTPStream(_FakeHTTPResponse(json))


async def test_ollama_payload_never_carries_reasoning_effort() -> None:
    """``reasoning_effort`` is an OpenAI-only argument and would be rejected."""
    engine = RAGEngine()
    engine.llm_provider = "ollama"
    http = _FakeHTTPClient()
    engine._http = http  # type: ignore[assignment]

    tokens = [token async for token in engine.generate_answer_stream("question")]

    assert tokens == ["local answer"]
    assert set(http.payloads[0]) == {"model", "prompt", "stream"}
