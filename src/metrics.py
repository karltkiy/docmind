"""Prometheus metrics registry and pure-ASGI instrumentation middleware."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .config import settings

REQUEST_COUNT = Counter(
    "docmind_http_requests_total",
    "Total HTTP requests handled.",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "docmind_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
)
CHAT_STREAMS = Counter(
    "docmind_chat_streams_total",
    "Chat completion streams by outcome.",
    ["outcome"],
)
INGESTION_JOBS = Counter(
    "docmind_ingestion_jobs_total",
    "Document ingestion jobs by outcome.",
    ["outcome"],
)
READY = Gauge(
    "docmind_ready",
    "Whether the API finished startup (1) or not (0).",
)
APP_INFO = Gauge(
    "docmind_app_info",
    "Static application information.",
    ["version", "environment", "embedding_provider", "llm_provider"],
)


def render_metrics() -> tuple[bytes, str]:
    """Return the Prometheus exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST


# Identifier-like path segments are collapsed so label cardinality stays bounded.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def route_label(path: str) -> str:
    """Collapse dynamic path segments (e.g. UUIDs) into ``{id}``."""
    return _UUID_RE.sub("{id}", path)


class MetricsMiddleware:
    """Pure-ASGI middleware that records request metrics.

    Deliberately implemented at the ASGI level rather than via
    ``BaseHTTPMiddleware``: the latter wraps and re-streams the response body,
    which is a known source of buffering/latency problems for Server-Sent
    Events. Here every ``http.response.body`` message passes straight through,
    so SSE keeps streaming token by token.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[..., Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or not settings.METRICS_ENABLED:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET"))
        label = route_label(str(scope.get("path", "unmatched")))
        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            REQUEST_COUNT.labels(method, label, str(status_code)).inc()
            REQUEST_LATENCY.labels(method, label).observe(time.perf_counter() - started)
