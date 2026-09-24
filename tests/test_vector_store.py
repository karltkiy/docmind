"""Unit tests for vector search result mapping."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from src.services.vector_store import SearchResult, VectorStore


class _FakeResult:
    """Stand-in for SQLAlchemy's ``Result`` exposing only ``all()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Minimal ``AsyncSession`` stand-in that captures the executed statement."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.statement: Any = None

    async def execute(self, statement: Any) -> _FakeResult:
        self.statement = statement
        return _FakeResult(self._rows)


async def test_search_converts_cosine_distance_to_similarity() -> None:
    chunk = SimpleNamespace(document_id=uuid.uuid4(), chunk_index=0, content="hello")
    session = _FakeSession([(chunk, 0.25)])

    results = await VectorStore.search_chunks(
        session=session,  # type: ignore[arg-type]
        query_embedding=[0.1, 0.2],
        top_k=1,
        document_ids=[uuid.uuid4()],
    )

    assert session.statement is not None
    assert results == [SearchResult(chunk=chunk, score=0.75)]
