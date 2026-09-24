"""Pydantic schemas for document endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ..db.models import DocumentStatus


class DocumentResponse(BaseModel):
    """Representation of a stored document and its processing state."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    status: DocumentStatus
    chunk_count: int = 0
    error: str | None = None
    created_at: datetime | None = None
