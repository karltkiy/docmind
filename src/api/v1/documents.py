"""Document upload, listing, status and deletion endpoints."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
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
from ..deps import enforce_upload_rate_limit

# Slack for multipart boundaries/headers when comparing Content-Length to the cap.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload without ever buffering more than ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


logger = logging.getLogger(__name__)

router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def _chunk_count(session: AsyncSession, document_id: uuid.UUID) -> int:
    """Return the number of embedded chunks stored for a document."""
    result = await session.execute(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
    )
    return int(result.scalar_one())


def _to_response(document: Document, chunk_count: int = 0) -> DocumentResponse:
    """Map an ORM document and its chunk count to the API schema."""
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        status=document.status,
        chunk_count=chunk_count,
        error=document.error,
        created_at=document.created_at,
    )


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(enforce_upload_rate_limit)],
    summary="Upload a document",
    description=(
        "Persist an uploaded document to shared storage and enqueue background "
        "parsing, chunking and embedding. Returns immediately with a "
        "`processing` document."
    ),
    responses={
        400: {"description": "The uploaded file is empty."},
        413: {"description": "The uploaded file exceeds the configured size limit."},
        415: {"description": "The file extension is not supported."},
        503: {"description": "The processing queue is unavailable."},
    },
)
async def upload_document(
    http_request: Request,
    file: Annotated[UploadFile, File(description="Document to ingest (.txt/.md/.csv/.json/.pdf).")],
    db: DbSession,
) -> DocumentResponse:
    """Store an uploaded document and enqueue background processing."""
    try:
        filename = normalize_filename(file.filename)
        suffix = validate_suffix(filename)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    max_bytes = settings.max_upload_bytes
    # Reject oversized bodies before reading them into memory when the client
    # declares a Content-Length.
    declared = http_request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > max_bytes + _MULTIPART_OVERHEAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.",
            )

    content = await _read_capped(file, max_bytes)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty."
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
    except Exception as exc:
        logger.exception("Failed to enqueue document %s", document.id)
        # Do not leave an orphaned file behind when the job was never queued.
        stored_path.unlink(missing_ok=True)
        document.status = DocumentStatus.FAILED
        document.error = "Failed to enqueue background processing job."
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Processing queue is unavailable. Please retry later.",
        ) from exc

    return _to_response(document, chunk_count=0)


@router.get(
    "",
    response_model=list[DocumentResponse],
    summary="List documents",
    description="List stored documents, newest first, with chunk counts.",
)
async def list_documents(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentResponse]:
    """List documents, newest first, in a single aggregated query."""
    result = await db.execute(
        select(Document, func.count(DocumentChunk.id))
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .group_by(Document.id)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_to_response(document, int(count)) for document, count in result.all()]


@router.get(
    "/{document_id}/status",
    response_model=DocumentResponse,
    summary="Get document status",
    description="Return the processing status and chunk count for one document.",
    responses={404: {"description": "Document not found."}},
)
async def get_document_status(document_id: uuid.UUID, db: DbSession) -> DocumentResponse:
    """Return the processing status of a single document."""
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return _to_response(document, await _chunk_count(db, document.id))


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
    description="Delete a document together with its embedded chunks and stored file.",
    responses={404: {"description": "Document not found."}},
)
async def delete_document(document_id: uuid.UUID, db: DbSession) -> None:
    """Delete a document, its chunks and its stored file."""
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    if document.file_path:
        try:
            Path(document.file_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove file %s", document.file_path)

    await db.delete(document)
    await db.commit()
