"""Arq background tasks for document parsing and embedding."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from ..config import settings
from ..db.base import SessionLocal, dispose_engine
from ..db.models import Document, DocumentChunk, DocumentStatus
from ..logging_config import configure_logging
from ..services.chunker import TextChunker
from ..services.parser import extract_text
from ..services.rag_engine import rag_engine
from .queue import redis_settings as build_redis_settings

configure_logging(settings.LOG_LEVEL, json_output=settings.LOG_JSON)

logger = logging.getLogger(__name__)


async def process_document_task(ctx: dict[str, Any], document_id: str) -> dict[str, Any]:
    """Parse a stored document, embed its chunks and persist them.

    Returns a small summary dict on success. On failure the document is marked
    as ``failed`` with the error message and the exception re-raised so arq can
    retry according to ``max_tries``.
    """
    doc_uuid = uuid.UUID(document_id)

    async with SessionLocal() as session:
        document = (
            await session.execute(select(Document).where(Document.id == doc_uuid))
        ).scalar_one_or_none()
        if document is None:
            raise ValueError(f"Document {document_id} not found.")

        filename = document.filename
        file_path = document.file_path
        logger.info("Processing document %s (%s)", doc_uuid, filename)

        try:
            if not file_path:
                raise ValueError("Document has no stored file to process.")

            text = extract_text(Path(file_path))
            if not text:
                raise ValueError("No extractable text found in document.")

            chunker = TextChunker(chunk_size=settings.CHUNK_SIZE, overlap=settings.CHUNK_OVERLAP)
            chunk_texts = chunker.chunk_text(text)
            if not chunk_texts:
                raise ValueError("Document produced no chunks.")

            embeddings = await rag_engine.get_embeddings(chunk_texts)
            if len(embeddings) != len(chunk_texts):
                raise ValueError("Embedding count does not match chunk count.")

            # Idempotent re-processing: drop any previously generated chunks.
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid)
            )
            for index, (content, embedding) in enumerate(zip(chunk_texts, embeddings, strict=True)):
                session.add(
                    DocumentChunk(
                        document_id=doc_uuid,
                        chunk_index=index,
                        content=content,
                        metadata_={"source": filename, "index": index},
                        embedding=embedding,
                    )
                )

            await session.execute(
                update(Document)
                .where(Document.id == doc_uuid)
                .values(status=DocumentStatus.COMPLETED, error=None)
            )
            await session.commit()
            logger.info("Finished document %s with %d chunks.", doc_uuid, len(chunk_texts))
            return {"document_id": str(doc_uuid), "chunks": len(chunk_texts)}

        except Exception as exc:  # noqa: BLE001 - persist failure details
            await session.rollback()
            logger.exception("Processing failed for document %s", doc_uuid)
            await session.execute(
                update(Document)
                .where(Document.id == doc_uuid)
                .values(status=DocumentStatus.FAILED, error=str(exc)[:2000])
            )
            await session.commit()
            raise


async def on_startup(ctx: dict[str, Any]) -> None:
    """Initialise shared resources for the worker process."""
    await rag_engine.startup()
    logger.info("Arq worker started.")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Release shared resources on worker shutdown."""
    await rag_engine.shutdown()
    await dispose_engine()
    logger.info("Arq worker stopped.")


class WorkerSettings:
    """Arq worker configuration."""

    functions = [process_document_task]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = build_redis_settings()
    max_jobs = 4
    job_timeout = 600
    keep_result = 3600
    max_tries = 3
