"""Pydantic schemas and SSE payload types for chat endpoints."""

from __future__ import annotations

from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """A grounded question over the indexed document corpus."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)
    document_ids: list[UUID] | None = None


class ChatSource(BaseModel):
    """A retrieved chunk cited as evidence for an answer."""

    document_id: UUID
    filename: str | None = None
    chunk_index: int
    score: float
    excerpt: str


class SseSources(TypedDict):
    """SSE frame carrying the retrieved sources."""

    type: Literal["sources"]
    content: list[dict[str, Any]]


class SseToken(TypedDict):
    """SSE frame carrying a single streamed answer token."""

    type: Literal["token"]
    content: str


class SseDone(TypedDict):
    """SSE frame signalling successful completion of the stream."""

    type: Literal["done"]


class SseError(TypedDict):
    """SSE frame signalling a failed stream, correlated by request id."""

    type: Literal["error"]
    message: str
    request_id: str


SsePayload = SseSources | SseToken | SseDone | SseError
