"""Pydantic schemas for document endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentBase(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)


class DocumentResponse(BaseModel):
    id: UUID
    filename: str
    status: str
    chunk_count: int = 0
    error: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class DocumentUploadResponse(DocumentResponse):
    """Response returned immediately after an upload is accepted."""
