"""Pydantic schemas for meta (root and health) endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class RootResponse(BaseModel):
    """Service identity and documentation pointer."""

    name: str
    version: str
    # ``None`` when the OpenAPI/Swagger surfaces are disabled (production).
    docs: str | None = None


class HealthResponse(BaseModel):
    """Liveness plus downstream dependency status."""

    status: Literal["healthy", "degraded"]
    database: bool
    redis: bool
    version: str
    environment: str
    provider_ready: bool
