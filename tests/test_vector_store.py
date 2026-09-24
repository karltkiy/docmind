"""Unit tests for vector store argument handling."""

from __future__ import annotations

import pytest

from src.services.vector_store import VectorStore


async def test_search_rejects_invalid_document_ids() -> None:
    with pytest.raises(ValueError):
        await VectorStore.search_chunks(
            session=None,  # type: ignore[arg-type]
            query_embedding=[0.0, 0.0],
            top_k=1,
            document_ids=["not-a-uuid"],
        )
