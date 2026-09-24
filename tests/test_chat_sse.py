"""Unit tests for SSE payload encoding."""

from __future__ import annotations

import json

from src.api.v1.chat import _serialize_sources, _sse
from src.services.vector_store import SearchResult


def test_sse_format() -> None:
    payload = _sse({"type": "token", "content": "hello"})
    assert payload.startswith("data: ")
    assert payload.endswith("\n\n")
    assert json.loads(payload[len("data: ") :].strip()) == {
        "type": "token",
        "content": "hello",
    }


class _Chunk:
    document_id = "11111111-1111-1111-1111-111111111111"

    def __init__(self, index: int, content: str) -> None:
        self.chunk_index = index
        self.content = content


def test_serialize_sources() -> None:
    results = [SearchResult(chunk=_Chunk(0, "some content"), score=0.81234)]
    sources = _serialize_sources(results, {})
    assert sources[0]["chunk_index"] == 0
    assert sources[0]["score"] == 0.8123
    assert sources[0]["excerpt"] == "some content"
