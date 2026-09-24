"""pgvector-backed similarity search over document chunks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import DocumentChunk


@dataclass(slots=True)
class SearchResult:
    """A single retrieval hit with its cosine similarity score."""

    chunk: DocumentChunk
    score: float


class VectorStore:
    """Stateless helper for vector similarity queries."""

    @staticmethod
    async def search_chunks(
        session: AsyncSession,
        query_embedding: list[float],
        top_k: int = 4,
        document_ids: list[str] | None = None,
    ) -> list[SearchResult]:
        """Return the ``top_k`` most similar chunks.

        Args:
            session: Active async DB session.
            query_embedding: Embedding of the user query.
            top_k: Maximum number of results.
            document_ids: Optional list of document UUID strings to constrain the
                search to.
        """
        distance = DocumentChunk.embedding.cosine_distance(query_embedding).label(
            "distance"
        )
        statement = select(DocumentChunk, distance)

        if document_ids:
            try:
                parsed_ids = [uuid.UUID(str(doc_id)) for doc_id in document_ids]
            except ValueError as exc:
                raise ValueError("document_ids must contain valid UUIDs.") from exc
            statement = statement.where(DocumentChunk.document_id.in_(parsed_ids))

        statement = statement.order_by(distance).limit(top_k)

        rows = (await session.execute(statement)).all()
        return [
            SearchResult(chunk=chunk, score=1.0 - float(dist))
            for chunk, dist in rows
        ]
