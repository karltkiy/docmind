"""JSON metrics endpoint backed by the in-process Prometheus registry."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from ...config import settings
from ...metrics import readiness, snapshot, uptime_seconds
from ...schemas.metrics import EndpointMetrics, LatencySummary, MetricsResponse

router = APIRouter(tags=["meta"])


def _resolve_version() -> str:
    """Return the installed package version, falling back for a source checkout."""
    try:
        return version("docmind")
    except PackageNotFoundError:
        return "0.0.0.dev0"


_APP_VERSION = _resolve_version()


@router.get(
    "",
    response_model=MetricsResponse,
    summary="Aggregated runtime metrics",
    description=(
        "Return a JSON snapshot of in-process HTTP, retrieval, chat and "
        "ingestion metrics. Latency values are in milliseconds; histogram "
        "percentiles are bucket-based approximations. No database, LLM or "
        "filesystem access is performed while serving this endpoint."
    ),
)
async def metrics_snapshot() -> MetricsResponse:
    """Return aggregated metrics without touching external dependencies."""
    data = snapshot()
    endpoints = sorted(
        (EndpointMetrics(**item) for item in data["endpoints"]),
        key=lambda item: (item.path, item.method),
    )
    return MetricsResponse(
        generated_at=datetime.now(UTC),
        version=_APP_VERSION,
        environment=settings.ENVIRONMENT,
        ready=readiness(),
        uptime_seconds=round(uptime_seconds(), 2),
        totals=dict(data["totals"]),
        chat_streams=dict(data["chat_streams"]),
        ingestion_jobs=dict(data["ingestion_jobs"]),
        latency_ms={
            name: LatencySummary(**summary) for name, summary in data["latency_ms"].items()
        },
        endpoints=endpoints,
    )
