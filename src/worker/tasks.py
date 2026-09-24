import asyncio
import logging
from typing import List
from .models import Document, DocumentChunk
from .base import SessionLocal
from .services.chunker import TextChunker
from .services.rag_engine import rag_engine
from .services.vector_store import VectorStore
from .config import settings

logger = logging.getLogger(__name__)

async def process_document_task(document_id: str):
    """
    Background task to process a document:
    1. Fetch document from DB
    2. Chunk text
    3. Generate embeddings
    4. Save chunks to DB
    """
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select

    async with SessionLocal() as session:
        # Fetch the document
        result = await session.execute(select(Document).where(Document.id == document_id))
        doc = result.scalar_one()
        
        logger.info(f"Starting processing for document {doc.id}")
        
        # In a real app, we'd fetch the raw content from a storage (S3/Local)
        # For this demo, we'll simulate the content retrieval.
        raw_content = f"This is the content for the document named {doc.filename}."
        
        chunker = TextChunker()
        chunks_text = chunker.chunk_text(raw_content)
        
        count = 0
        for i, text in enumerate(chunks_text):
            # Generate embedding
            embedding = await rag_engine.get_embedding(text)
            
            # Create chunk record
            new_chunk = DocumentChunk(
                document_id=doc.id,
                chunk_index=i,
                content=text,
                metadata_={"source": doc.filename, "index": i},
                embedding=embedding
            )
            session.add(new_chunk)
            count += 1
        
        doc.status = "completed"
        session.commit()
        logger.info(f"Finished processing document {doc.id} with {count} chunks.")

class WorkerSettings:
    """
    Arq worker settings.
    """
    functions = {
        "process_document": process_document_task,
    }
    # Other settings like redis_url can be pulled from config
    # but Arq usually takes them from environment or passed in.
