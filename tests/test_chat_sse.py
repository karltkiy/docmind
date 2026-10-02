"""Unit tests for SSE payload encoding and source serialization."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from src.api.v1 import chat as chat_module
from src.api.v1.chat import _serialize_sources, _sse
from src.db.base import get_session_factory
from src.main import app
from src.schemas.chat import ChatSource
from src.services.rag_engine import rag_engine
from src.services.vector_store import SearchResult


def test_sse_format() -> None:
    payload = _sse({"type": "token", "content": "hello"})
    assert payload.startswith("data: ")
    assert payload.endswith("\n\n")
    assert json.loads(payload[len("data: ") :].strip()) == {
        "type": "token",
        "content": "hello",
    }


def _chunk(document_id: uuid.UUID, index: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(document_id=document_id, chunk_index=index, content=content)


def test_serialize_sources_returns_typed_models() -> None:
    document_id = uuid.uuid4()
    results = [SearchResult(chunk=_chunk(document_id, 0, "some content"), score=0.81234)]

    sources = _serialize_sources(results, {document_id: "guide.pdf"})

    assert len(sources) == 1
    source = sources[0]
    assert isinstance(source, ChatSource)
    assert source.document_id == document_id
    assert source.filename == "guide.pdf"
    assert source.chunk_index == 0
    assert source.score == 0.8123
    assert source.excerpt == "some content"


class _FakeSession:
    """Async-context stand-in for a SQLAlchemy session."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_chat_endpoint_streams_sse_frames_and_headers(monkeypatch) -> None:
    """The real app must stream ordered SSE frames with SSE headers intact."""

    async def fake_get_embedding(text: str) -> list[float]:
        return [0.0]

    async def fake_generate(prompt: str) -> AsyncIterator[str]:
        for token in ("Hello", " ", "world"):
            yield token

    async def fake_search(**kwargs: object) -> list[SearchResult]:
        return []

    monkeypatch.setattr(rag_engine, "get_embedding", fake_get_embedding)
    monkeypatch.setattr(rag_engine, "generate_answer_stream", fake_generate)
    monkeypatch.setattr(chat_module.VectorStore, "search_chunks", staticmethod(fake_search))

    def fake_factory() -> _FakeSession:
        return _FakeSession()

    app.dependency_overrides[get_session_factory] = lambda: fake_factory
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/chat/completions", json={"query": "hi", "top_k": 1}
            )
    finally:
        app.dependency_overrides.pop(get_session_factory, None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"

    body = response.text
    assert '"type": "sources"' in body
    assert "Hello" in body and "world" in body
    assert '"type": "done"' in body
    # Frames must be emitted in the documented order.
    assert body.index('"type": "sources"') < body.index('"type": "token"')
    assert body.index('"type": "token"') < body.index('"type": "done"')
