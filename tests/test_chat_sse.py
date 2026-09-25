"""Unit tests for SSE payload encoding and source serialization."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from src.api.v1.chat import _serialize_sources, _sse
from src.schemas.chat import ChatSource
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
