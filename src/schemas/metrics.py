"""Pydantic schemas for the JSON metrics endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LatencySummary(BaseModel):
    """Approximate latency distribution summary, in milliseconds."""

    count: int
    mean: float
    p50: float
    p95: float
    p99: float
    max: float


class EndpointMetrics(BaseModel):
    """Per-endpoint request counts and latency summary."""

    method: str
    path: str
    status_counts: dict[str, int]
    latency_ms: LatencySummary


class MetricsResponse(BaseModel):
    """Aggregated in-process metrics snapshot with service metadata."""

    generated_at: datetime
    version: str
    environment: str
    ready: bool
    uptime_seconds: float
    totals: dict[str, int]
    chat_streams: dict[str, int]
    ingestion_jobs: dict[str, int]
    latency_ms: dict[str, LatencySummary]
    endpoints: list[EndpointMetrics]
