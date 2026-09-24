"""Pydantic schemas for chat endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)
    document_ids: list[str] | None = None


class ChatSource(BaseModel):
    document_id: str
    filename: str | None = None
    chunk_index: int
    score: float
    excerpt: str
