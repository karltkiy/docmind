"""Pydantic schemas for meta (root and health) endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class RootResponse(BaseModel):
    """Service identity and documentation pointer."""

    name: str
    version: str
    docs: str


class HealthResponse(BaseModel):
    """Liveness plus downstream dependency status."""

    status: Literal["healthy", "degraded"]
    database: bool
    redis: bool
