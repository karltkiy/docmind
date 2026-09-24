"""Document upload, listing, status and deletion endpoints."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...db.base import get_db_session
from ...db.models import Document, DocumentChunk, DocumentStatus
from ...schemas.document import DocumentResponse
from ...services.parser import (
    UnsupportedFileTypeError,
    normalize_filename,
    validate_suffix,
)
from ...worker.queue import get_redis_pool
from ...worker.tasks import process_document_task

logger = logging.getLogger(__name__)

router = APIRouter()


async def _chunk_count(session: AsyncSession, document_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
    )
    return int(result.scalar_one())


def _to_response(document: Document, chunk_count: int = 0) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        status=document.status.value
        if isinstance(document.status, DocumentStatus)
        else str(document.status),
        chunk_count=chunk_count,
        error=document.error,
        created_at=document.created_at,
    )


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentResponse:
    """Store an uploaded document and enqueue background processing."""
    try:
        filename = normalize_filename(file.filename)
        suffix = validate_suffix(filename)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty."
        )
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.",
        )

    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    document_id = uuid.uuid4()
    stored_path = upload_dir / f"{document_id}{suffix}"
    stored_path.write_bytes(content)

    document = Document(
        id=document_id,
        filename=filename,
        file_path=str(stored_path),
        content_type=file.content_type,
        status=DocumentStatus.PROCESSING,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    try:
        pool = await get_redis_pool()
        await pool.enqueue_job(process_document_task.__name__, str(document.id))
    except Exception as exc:  # noqa: BLE001 - surface queue outages clearly
        logger.exception("Failed to enqueue document %s", document.id)
        document.status = DocumentStatus.FAILED
        document.error = "Failed to enqueue background processing job."
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Processing queue is unavailable. Please retry later.",
        ) from exc

    return _to_response(document, chunk_count=0)


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    db: AsyncSession = Depends(get_db_session),
) -> list[DocumentResponse]:
    """List all documents, newest first."""
    result = await db.execute(select(Document).order_by(Document.created_at.desc()))
    documents = result.scalars().all()
    return [_to_response(doc, await _chunk_count(db, doc.id)) for doc in documents]


@router.get("/{document_id}/status", response_model=DocumentResponse)
async def get_document_status(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> DocumentResponse:
    """Return the processing status of a single document."""
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        )
    return _to_response(document, await _chunk_count(db, document.id))


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a document, its chunks and its stored file."""
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        )

    if document.file_path:
        try:
            Path(document.file_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove file %s", document.file_path)

    await db.delete(document)
    await db.commit()
