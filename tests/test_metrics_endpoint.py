"""Tests for the JSON metrics endpoint and the metrics snapshot helper."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, Histogram

from src import metrics as metrics_module
from src.config import settings
from src.main import app

_EXPECTED_TOP_LEVEL = {
    "generated_at",
    "version",
    "environment",
    "ready",
    "uptime_seconds",
    "totals",
    "chat_streams",
    "ingestion_jobs",
    "latency_ms",
    "endpoints",
}
_EXPECTED_LATENCIES = {"retrieval", "chat_time_to_first_token", "ingestion"}
_EXPECTED_SUMMARY_FIELDS = {"count", "mean", "p50", "p95", "p99", "max"}


async def test_metrics_endpoint_returns_valid_schema() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.json()

    assert set(body) == _EXPECTED_TOP_LEVEL
    assert set(body["totals"]) == {
        "http_requests_total",
        "chat_streams_total",
        "ingestion_jobs_total",
    }
    assert set(body["chat_streams"]) == {"success", "error"}
    assert set(body["ingestion_jobs"]) == {"success", "failure"}
    assert set(body["latency_ms"]) == _EXPECTED_LATENCIES
    for summary in body["latency_ms"].values():
        assert set(summary) == _EXPECTED_SUMMARY_FIELDS
        assert isinstance(summary["count"], int)

    assert isinstance(body["endpoints"], list)
    for endpoint in body["endpoints"]:
        assert set(endpoint) == {"method", "path", "status_counts", "latency_ms"}
    endpoint_keys = [(item["path"], item["method"]) for item in body["endpoints"]]
    assert endpoint_keys == sorted(endpoint_keys)

    assert body["environment"] == settings.ENVIRONMENT
    assert body["version"]
    assert body["uptime_seconds"] >= 0.0


async def test_metrics_endpoint_requires_api_key_when_configured(monkeypatch) -> None:
    """The endpoint follows the same auth behaviour as the other v1 routes."""
    monkeypatch.setattr(settings, "API_KEY", "top-secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/metrics")

    assert response.status_code == 401


def test_snapshot_handles_empty_registry(monkeypatch) -> None:
    """An empty registry yields zeros and empty lists instead of raising."""
    monkeypatch.setattr(metrics_module, "REGISTRY", CollectorRegistry())

    data = metrics_module.snapshot()

    assert data["totals"] == {
        "http_requests_total": 0,
        "chat_streams_total": 0,
        "ingestion_jobs_total": 0,
    }
    assert data["chat_streams"] == {"success": 0, "error": 0}
    assert data["ingestion_jobs"] == {"success": 0, "failure": 0}
    assert data["endpoints"] == []
    assert set(data["latency_ms"]) == _EXPECTED_LATENCIES
    for summary in data["latency_ms"].values():
        assert summary == {
            "count": 0,
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }


def test_snapshot_percentiles_are_monotonic(monkeypatch) -> None:
    """Bucket-interpolated percentiles must satisfy p50 <= p95 <= p99 <= max."""
    registry = CollectorRegistry()
    retrieval = Histogram(
        "docmind_retrieval_duration_seconds",
        "Synthetic retrieval latency.",
        ["outcome"],
        buckets=metrics_module.RETRIEVAL_BUCKETS,
        registry=registry,
    )
    for value in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0):
        retrieval.labels(outcome="success").observe(value)

    monkeypatch.setattr(metrics_module, "REGISTRY", registry)

    summary = metrics_module.snapshot()["latency_ms"]["retrieval"]

    assert summary["count"] == 7
    assert summary["mean"] > 0.0
    assert summary["p50"] <= summary["p95"] <= summary["p99"] <= summary["max"]


def test_snapshot_percentiles_when_values_exceed_the_last_bucket(monkeypatch) -> None:
    """Slow observations must not leave ``max`` below p99 (the ``+Inf`` bucket)."""
    registry = CollectorRegistry()
    retrieval = Histogram(
        "docmind_retrieval_duration_seconds",
        "Synthetic slower-than-bucket retrieval latency.",
        ["outcome"],
        buckets=metrics_module.RETRIEVAL_BUCKETS,
        registry=registry,
    )
    retrieval.labels(outcome="success").observe(10.0)

    monkeypatch.setattr(metrics_module, "REGISTRY", registry)

    summary = metrics_module.snapshot()["latency_ms"]["retrieval"]

    assert summary["count"] == 1
    assert summary["max"] > 0.0
    assert summary["p50"] <= summary["p95"] <= summary["p99"] <= summary["max"]


async def test_metrics_endpoint_accepts_configured_api_key(monkeypatch) -> None:
    """A valid key must still be accepted once authentication is configured."""
    monkeypatch.setattr(settings, "API_KEY", "test-api-key")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/metrics", headers={settings.API_KEY_HEADER: "test-api-key"}
        )

    assert response.status_code == 200


async def test_prometheus_endpoint_disabled_returns_404_with_timing_header(monkeypatch) -> None:
    """Disabling metrics hides /metrics but keeps the timing header on the 404."""
    monkeypatch.setattr(settings, "METRICS_ENABLED", False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 404
    assert float(response.headers["x-response-time-ms"]) >= 0.0
