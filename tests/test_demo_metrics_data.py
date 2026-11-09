"""Unit tests for the demo metrics data layer.

No Streamlit, no network: ``requests.get`` is always monkeypatched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from demo import metrics_data

FIXED_NOW = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)


def _document(**overrides: Any) -> dict[str, Any]:
    """A minimal but complete metrics_static fixture document."""
    document: dict[str, Any] = {
        "schema_version": 1,
        "kind": "metrics_static",
        "generated_at": "2026-01-01T12:00:00+00:00",
        "provenance": {
            "git_commit": "c6255eb" + "0" * 33,
            "git_dirty": False,
            "source_command": "python -m benchmarks.static_metrics --with-coverage",
            "source_artifacts": ["benchmarks/static_metrics.json"],
            "environment": {
                "python": "3.14.8",
                "platform": "Windows-11-10.0.26300-SP0",
                "processor": "AMD64 Family 25",
                "cpu_count": 12,
            },
        },
        "limitations": ["latency measured on a single machine, not an SLA"],
        "payload": {"tests": {"collected": 120}},
    }
    document.update(overrides)
    return document


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    directory = tmp_path / "fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / metrics_data.FIXTURE_FILES["metrics_static"]
    path.write_text(json.dumps(document), encoding="utf-8")
    return directory


def _drop(document: dict[str, Any], dotted: str) -> dict[str, Any]:
    """Return a copy of ``document`` without the dotted path ``a.b.c``."""
    clone = json.loads(json.dumps(document))
    keys = dotted.split(".")
    target: Any = clone
    for key in keys[:-1]:
        target = target[key]
    del target[keys[-1]]
    return clone


class _FakeResponse:
    """Stand-in for :class:`requests.Response`."""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _live_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": "2026-11-09T15:30:00+00:00",
        "version": "1.0.1",
        "environment": "development",
        "ready": True,
        "uptime_seconds": 120.0,
        "totals": {"http_requests_total": 12, "http_responses_total": 12},
        "chat_streams": {"started": 1, "completed": 1, "failed": 0},
        "ingestion_jobs": {"completed": 2, "failed": 0},
        "latency_ms": {
            "retrieval": {
                "count": 1,
                "mean": 10.0,
                "p50": 10.0,
                "p95": 11.0,
                "p99": 11.0,
                "max": 11.0,
            }
        },
        "endpoints": [
            {
                "method": "GET",
                "path": "/health",
                "status_counts": {"200": 12},
                "latency_ms": {
                    "count": 12,
                    "mean": 1.0,
                    "p50": 1.0,
                    "p95": 2.0,
                    "p99": 2.0,
                    "max": 2.0,
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Fixture validation
# ---------------------------------------------------------------------------
def test_complete_fixture_loads(tmp_path: Path) -> None:
    directory = _write(tmp_path, _document())

    fixture = metrics_data.load_fixture("metrics_static", directory=directory)

    assert fixture.kind == "metrics_static"
    assert fixture.schema_version == 1
    assert fixture.generated_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert fixture.provenance.environment["cpu_count"] == 12
    assert fixture.limitations
    assert fixture.payload["tests"]["collected"] == 120


@pytest.mark.parametrize(
    "dotted",
    [
        "schema_version",
        "kind",
        "generated_at",
        "provenance",
        "provenance.git_commit",
        "provenance.git_dirty",
        "provenance.source_command",
        "provenance.environment",
        "provenance.environment.python",
        "provenance.environment.platform",
        "provenance.environment.processor",
        "provenance.environment.cpu_count",
        "limitations",
        "payload",
    ],
)
def test_missing_provenance_field_is_rejected(tmp_path: Path, dotted: str) -> None:
    directory = _write(tmp_path, _drop(_document(), dotted))

    with pytest.raises(metrics_data.FixtureError, match="missing"):
        metrics_data.load_fixture("metrics_static", directory=directory)


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    directory = _write(tmp_path, _document(schema_version=99))

    with pytest.raises(metrics_data.FixtureError, match="unsupported schema_version"):
        metrics_data.load_fixture("metrics_static", directory=directory)


def test_empty_limitations_are_rejected(tmp_path: Path) -> None:
    directory = _write(tmp_path, _document(limitations=[]))

    with pytest.raises(metrics_data.FixtureError, match="limitations"):
        metrics_data.load_fixture("metrics_static", directory=directory)


def test_kind_mismatch_is_rejected(tmp_path: Path) -> None:
    directory = _write(tmp_path, _document(kind="microbenchmarks"))

    with pytest.raises(metrics_data.FixtureError, match="expected kind"):
        metrics_data.load_fixture("metrics_static", directory=directory)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(metrics_data.FixtureError, match="missing"):
        metrics_data.load_fixture("metrics_static", directory=tmp_path / "empty")


def test_broken_json_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "fixtures"
    directory.mkdir()
    (directory / metrics_data.FIXTURE_FILES["metrics_static"]).write_text(
        "{not json", encoding="utf-8"
    )

    with pytest.raises(metrics_data.FixtureError, match="not valid JSON"):
        metrics_data.load_fixture("metrics_static", directory=directory)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(metrics_data.FixtureError, match="Unknown fixture kind"):
        metrics_data.load_fixture("no_such_kind")


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------
def test_age_days_uses_the_supplied_reference(tmp_path: Path) -> None:
    directory = _write(tmp_path, _document())
    fixture = metrics_data.load_fixture("metrics_static", directory=directory)

    assert metrics_data.age_days(fixture.generated_at, now=FIXED_NOW) == pytest.approx(30.0)


def test_is_stale_threshold(tmp_path: Path) -> None:
    directory = _write(tmp_path, _document())
    fixture = metrics_data.load_fixture("metrics_static", directory=directory)

    assert metrics_data.is_stale(fixture, 10, now=FIXED_NOW) is True
    assert metrics_data.is_stale(fixture, 60, now=FIXED_NOW) is False


def test_is_stale_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write(tmp_path, _document())
    fixture = metrics_data.load_fixture("metrics_static", directory=directory)

    monkeypatch.setenv(metrics_data.MAX_AGE_ENV_VAR, "5")
    assert metrics_data.is_stale(fixture, now=FIXED_NOW) is True

    monkeypatch.setenv(metrics_data.MAX_AGE_ENV_VAR, "not-a-number")
    assert metrics_data.is_stale(fixture, now=FIXED_NOW) is False


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "n/a"),
        (0.135, "0.135 ms"),
        (46.7, "46.70 ms"),
        (2238.4, "2238.4 ms"),
    ],
)
def test_format_ms(value: float | None, expected: str) -> None:
    assert metrics_data.format_ms(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "n/a"),
        (-1, "n/a"),
        (7, "7s"),
        (65, "1m 05s"),
        (3700, "1h 01m"),
        (90000, "1d 01h"),
    ],
)
def test_format_uptime(value: float | None, expected: str) -> None:
    assert metrics_data.format_uptime(value) == expected


def test_format_percent() -> None:
    assert metrics_data.format_percent(1.0) == "100.0%"
    assert metrics_data.format_percent(0.9545, digits=2) == "95.45%"
    assert metrics_data.format_percent(None) == "n/a"


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------
def _patch_get(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> None:
    monkeypatch.setattr(metrics_data, "API_BASE_URL", "http://metrics.test/api/v1")
    monkeypatch.setattr(requests, "get", behaviour)


def test_load_live_metrics_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, lambda *a, **k: _FakeResponse(200, _live_payload()))

    snapshot = metrics_data.load_live_metrics()

    assert snapshot is not None
    assert snapshot.version == "1.0.1"
    assert snapshot.endpoints[0].requests == 12
    assert snapshot.counters_disabled is False
    assert metrics_data.last_status() == 200
    assert metrics_data.last_error() is None


def test_load_live_metrics_returns_none_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "unit-test-secret-value"
    monkeypatch.setenv(metrics_data.API_KEY_ENV_VAR, secret)
    _patch_get(monkeypatch, lambda *a, **k: _FakeResponse(401))

    assert metrics_data.load_live_metrics() is None
    assert metrics_data.last_status() == 401
    error = metrics_data.last_error() or ""
    assert "DOCMIND_API_KEY" in error
    assert secret not in error


def test_load_live_metrics_returns_none_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise requests.ConnectionError("connection refused")

    _patch_get(monkeypatch, boom)

    assert metrics_data.load_live_metrics() is None
    assert metrics_data.last_status() is None
    assert "ConnectionError" in (metrics_data.last_error() or "")


def test_load_live_metrics_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> None:
        raise requests.Timeout("too slow")

    _patch_get(monkeypatch, slow)

    assert metrics_data.load_live_metrics(timeout=1) is None
    assert "Timed out" in (metrics_data.last_error() or "")


@pytest.mark.parametrize(
    "payload",
    [
        {"generated_at": "not-a-date"},
        {"version": "1.0.1"},
        {"ready": "yes"},
    ],
)
def test_load_live_metrics_returns_none_on_bad_schema(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    _patch_get(monkeypatch, lambda *a, **k: _FakeResponse(200, payload))

    assert metrics_data.load_live_metrics() is None
    assert "unexpected schema" in (metrics_data.last_error() or "").lower()


def test_counters_disabled_when_process_reports_no_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _live_payload(totals={"http_requests_total": 0}, uptime_seconds=60.0)
    _patch_get(monkeypatch, lambda *a, **k: _FakeResponse(200, payload))

    snapshot = metrics_data.load_live_metrics()

    assert snapshot is not None
    assert snapshot.counters_disabled is True


def test_counters_not_disabled_right_after_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _live_payload(totals={"http_requests_total": 0}, uptime_seconds=1.0)
    _patch_get(monkeypatch, lambda *a, **k: _FakeResponse(200, payload))

    snapshot = metrics_data.load_live_metrics()

    assert snapshot is not None
    assert snapshot.counters_disabled is False


def test_data_layer_never_calls_the_chat_endpoint() -> None:
    source = Path(metrics_data.__file__).read_text(encoding="utf-8")

    assert "chat/completions" not in source
    assert "requests.post" not in source
