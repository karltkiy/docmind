"""Prometheus metrics registry and pure-ASGI instrumentation middleware."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .config import settings

# Buckets are declared explicitly (rather than relying on defaults) so the JSON
# snapshot can derive bucket-based percentiles deterministically. Retrieval and
# time-to-first-token buckets span 5 ms .. 5 s; ingestion jobs are slower and
# therefore use a wider ladder.
RETRIEVAL_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)
TTFT_BUCKETS: tuple[float, ...] = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)
INGESTION_BUCKETS: tuple[float, ...] = (
    0.1,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

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
RETRIEVAL_DURATION = Histogram(
    "docmind_retrieval_duration_seconds",
    "Vector retrieval latency in seconds.",
    ["outcome"],
    buckets=RETRIEVAL_BUCKETS,
)
CHAT_TTFT = Histogram(
    "docmind_chat_time_to_first_token_seconds",
    "Time from chat request start to the first streamed token, in seconds.",
    ["outcome"],
    buckets=TTFT_BUCKETS,
)
INGESTION_DURATION = Histogram(
    "docmind_ingestion_duration_seconds",
    "Document ingestion duration in seconds.",
    ["status"],
    buckets=INGESTION_BUCKETS,
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
# Wall-clock unix timestamp of process start. It is exported as a gauge for
# Prometheus consumers, while the in-app uptime below uses a monotonic clock so
# NTP or manual clock changes cannot make uptime jump backwards.
STARTED_AT = Gauge(
    "docmind_started_at_seconds",
    "Unix timestamp (seconds) recorded at process start.",
)
_STARTED_AT_EPOCH = time.time()
_STARTED_AT_MONOTONIC = time.monotonic()
STARTED_AT.set(_STARTED_AT_EPOCH)


def uptime_seconds() -> float:
    """Return seconds since process start measured on a monotonic clock."""
    return time.monotonic() - _STARTED_AT_MONOTONIC


def readiness() -> bool:
    """Return the current value of the ``docmind_ready`` gauge."""
    return bool(REGISTRY.get_sample_value("docmind_ready") or 0.0)


def render_metrics() -> tuple[bytes, str]:
    """Return the Prometheus exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST


# Identifier-like path segments are collapsed so label cardinality stays bounded.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_RESPONSE_TIME_HEADER = b"x-response-time-ms"


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

    The ``X-Response-Time-Ms`` header is added unconditionally — including when
    ``METRICS_ENABLED`` is ``False``. The flag only governs whether samples are
    written to the Prometheus registry; operators still get the timing header
    when scraping is disabled (e.g. in tests or local runs). The value is
    measured up to ``http.response.start`` because, by definition, headers must
    be sent before the body finishes streaming.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[..., Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
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
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                headers = list(message.get("headers") or [])
                headers.append((_RESPONSE_TIME_HEADER, str(elapsed_ms).encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if settings.METRICS_ENABLED:
                REQUEST_COUNT.labels(method, label, str(status_code)).inc()
                REQUEST_LATENCY.labels(method, label).observe(time.perf_counter() - started)


# ----------------------------------------------------------------------
# JSON snapshot helpers
# ----------------------------------------------------------------------


def _all_samples() -> list[Any]:
    """Return every sample currently held by the default registry."""
    return [sample for family in REGISTRY.collect() for sample in family.samples]


def _samples_for(metric_name: str) -> list[Any]:
    """Return samples belonging to ``metric_name`` (including histogram buckets).

    Matching by prefix keeps this robust across prometheus-client versions that
    may name the sample family slightly differently from the metric.
    """
    prefix = f"{metric_name}_"
    return [
        sample
        for sample in _all_samples()
        if sample.name == metric_name or sample.name.startswith(prefix)
    ]


def _counter_total(metric_name: str) -> int:
    """Sum a counter across every label combination."""
    return int(
        sum(sample.value for sample in _samples_for(metric_name) if sample.name == metric_name)
    )


def _labeled_counter(metric_name: str, label_name: str, label_value: str) -> int:
    """Read a single counter series by one label value, defaulting to zero."""
    for sample in _samples_for(metric_name):
        if sample.name == metric_name and sample.labels.get(label_name) == label_value:
            return int(sample.value)
    return 0


def _bucket_percentile(buckets: list[tuple[float, float]], count: float, quantile: float) -> float:
    """Approximate a quantile from cumulative histogram buckets.

    The target rank is located in the bucket whose cumulative count first
    reaches it, and the value is linearly interpolated between the previous and
    current bucket boundaries. This is an approximation: exact quantiles would
    require retaining every observation.
    """
    if count <= 0:
        return 0.0
    target = quantile * count
    lower_bound = 0.0
    lower_count = 0.0
    for upper_bound, cumulative in buckets:
        if cumulative >= target:
            if not math.isfinite(upper_bound):
                # The target falls in the ``+Inf`` bucket; the previous finite
                # boundary is the best available upper estimate.
                return lower_bound
            span = cumulative - lower_count
            if span <= 0:
                return upper_bound
            fraction = (target - lower_count) / span
            return lower_bound + fraction * (upper_bound - lower_bound)
        lower_bound = upper_bound
        lower_count = cumulative
    return lower_bound


def _latency_summary(
    count: float, total_seconds: float, buckets: Mapping[float, float]
) -> dict[str, float]:
    """Build a millisecond latency summary from cumulative bucket counts."""
    if count <= 0:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}

    ordered = sorted(buckets.items())
    summary: dict[str, float] = {
        "count": int(count),
        "mean": round(total_seconds / count * 1000.0, 2),
    }
    for name, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        summary[name] = round(_bucket_percentile(ordered, count, quantile) * 1000.0, 2)
    # ``max`` is approximated by the highest finite boundary that has samples;
    # the true maximum is unobservable once values fall into the ``+Inf`` bucket.
    finite_bounds = [
        upper for upper, cumulative in ordered if math.isfinite(upper) and cumulative > 0
    ]
    if finite_bounds:
        summary["max"] = round(max(finite_bounds) * 1000.0, 2)
    else:
        # Every observation exceeded the last finite bucket: the percentiles
        # already collapsed to that bucket's lower bound, so reuse p99 instead
        # of reporting 0 and breaking ``p50 <= p95 <= p99 <= max``.
        summary["max"] = summary["p99"]
    return summary


def _histogram_summary(samples: list[Any]) -> dict[str, float]:
    """Aggregate histogram samples (optionally pre-filtered) into a summary."""
    count = 0.0
    total = 0.0
    buckets: dict[float, float] = {}
    for sample in samples:
        if sample.name.endswith("_bucket"):
            try:
                upper = float(sample.labels.get("le", "+Inf"))
            except (TypeError, ValueError):
                continue
            buckets[upper] = buckets.get(upper, 0.0) + float(sample.value)
        elif sample.name.endswith("_count"):
            count += float(sample.value)
        elif sample.name.endswith("_sum"):
            total += float(sample.value)
    return _latency_summary(count, total, buckets)


def snapshot() -> dict[str, Any]:
    """Return a JSON-friendly snapshot of the in-process metrics registry.

    All latency values are reported in **milliseconds**. Histogram percentiles
    are **approximate**: they are derived from cumulative bucket counts using
    linear interpolation, so they are bounded by the configured bucket edges
    rather than reflecting exact observations. The snapshot contains only
    aggregate counters and never request payloads, secrets or filesystem paths.

    Missing metrics are reported as zeros and empty lists; the function never
    raises when the registry has no data yet.
    """
    http_samples = _samples_for("docmind_http_request_duration_seconds")
    request_samples = _samples_for("docmind_http_requests_total")

    endpoint_pairs = {
        (
            str(sample.labels.get("method", "")),
            str(sample.labels.get("path", "")),
        )
        for sample in http_samples
        if "method" in sample.labels and "path" in sample.labels
    }

    endpoints: list[dict[str, Any]] = []
    for method, path in sorted(endpoint_pairs, key=lambda pair: (pair[1], pair[0])):
        latency_samples = [
            sample
            for sample in http_samples
            if sample.labels.get("method") == method and sample.labels.get("path") == path
        ]
        status_counts = {
            str(sample.labels.get("status", "")): int(sample.value)
            for sample in request_samples
            if sample.name == "docmind_http_requests_total"
            and sample.labels.get("method") == method
            and sample.labels.get("path") == path
        }
        endpoints.append(
            {
                "method": method,
                "path": path,
                "status_counts": status_counts,
                "latency_ms": _histogram_summary(latency_samples),
            }
        )

    return {
        "totals": {
            "http_requests_total": _counter_total("docmind_http_requests_total"),
            "chat_streams_total": _counter_total("docmind_chat_streams_total"),
            "ingestion_jobs_total": _counter_total("docmind_ingestion_jobs_total"),
        },
        "chat_streams": {
            "success": _labeled_counter("docmind_chat_streams_total", "outcome", "success"),
            "error": _labeled_counter("docmind_chat_streams_total", "outcome", "error"),
        },
        "ingestion_jobs": {
            "success": _labeled_counter("docmind_ingestion_jobs_total", "outcome", "success"),
            "failure": _labeled_counter("docmind_ingestion_jobs_total", "outcome", "failure"),
        },
        "latency_ms": {
            "retrieval": _histogram_summary(_samples_for("docmind_retrieval_duration_seconds")),
            "chat_time_to_first_token": _histogram_summary(
                _samples_for("docmind_chat_time_to_first_token_seconds")
            ),
            "ingestion": _histogram_summary(_samples_for("docmind_ingestion_duration_seconds")),
        },
        "endpoints": endpoints,
    }
