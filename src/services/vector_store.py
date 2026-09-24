from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func
from .models import DocumentChunk
from .config import settings

class VectorStore:
    @staticmethod
    async def search_chunks(
        session: AsyncSession,
        query_embedding: list[float],
        top_k: int = 4,
        document_ids: Optional[list[str]] = None
    ) -> List[DocumentChunk]:
        """
        Perform a vector similarity search using pgvector.
        
        Args:
            session: The SQLAlchemy AsyncSession.
            query_embedding: The embedding of the user's query.
            top_k: Number of top results to return.
            document_ids: Optional list of document IDs to filter by.
        """
        # Construct the base query
        # pgvector's <-> operator is for L2 distance, <=> is for cosine distance.
        # Since we are using cosine similarity for RAG, we use <=>
        query = select(DocumentChunk).order_by(
            DocumentChunk.embedding.cosine_distance(query_embedding)
        ).limit(top_k)

        if document_ids:
            query = query.where(DocumentChunk.document_id.in_(document_ids))

        result = await session.execute(query)
        return result.scalars().all()
