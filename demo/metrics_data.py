"""Pure data layer behind the demo's *Metrics & evidence* view.

The module deliberately does not import Streamlit: it is reused by the
maintainer exporter (:mod:`demo.export_fixtures`), by pytest and by the
Streamlit page (:mod:`demo.metrics_page`) without dragging the UI runtime
into any of them.

Two kinds of numbers reach the page:

* **Committed fixtures** (``demo/fixtures/*.json``) written by
  :mod:`demo.export_fixtures` from real measurement artifacts —
  ``benchmarks/static_metrics.json``, ``benchmarks/results/latest.json`` and
  the newest ``eval/reports/retrieval-fixed-*.json``. Every fixture has to
  carry provenance metadata and a non-empty ``limitations`` list; a fixture
  that lost either is rejected instead of being rendered as fact.
* **A live snapshot** from ``GET /api/v1/metrics``. That endpoint reads the
  in-process Prometheus registry only — no database, no LLM, no filesystem —
  so rendering the page costs nothing in provider tokens.

This module never contacts the chat-completions route and never logs, echoes or
returns the API key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Directory holding the committed fixtures.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Fixture kind -> file name inside :data:`FIXTURES_DIR`.
FIXTURE_FILES: dict[str, str] = {
    "metrics_static": "metrics_static.json",
    "retrieval_eval": "retrieval_eval.json",
    "microbenchmarks": "microbenchmarks.json",
}

#: Fixture schema versions this build knows how to read.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

#: Age after which a fixture is reported as stale in the UI.
DEFAULT_MAX_AGE_DAYS = 120

#: Environment override for :data:`DEFAULT_MAX_AGE_DAYS`.
MAX_AGE_ENV_VAR = "DOCMIND_FIXTURE_MAX_AGE_DAYS"

#: Environment variable holding the (optional) API key.
API_KEY_ENV_VAR = "DOCMIND_API_KEY"

#: Environment variable holding the API base URL (``.../api/v1``).
API_URL_ENV_VAR = "DOCMIND_API_URL"

#: Base URL of the API. Read once at import; tests patch the module attribute.
API_BASE_URL = os.getenv(API_URL_ENV_VAR, "http://localhost:8000/api/v1").rstrip("/")

#: Timeout, in seconds, for the live metrics request.
REQUEST_TIMEOUT = 5.0

#: Upstream the snapshot endpoint is reached through (kept for assertions/tests).
METRICS_PATH = "/metrics"

#: Number of seconds of uptime after which an all-zero counter is suspicious.
COUNTERS_DISABLED_AFTER_SECONDS = 5.0


class FixtureError(RuntimeError):
    """Raised when a fixture is missing, unreadable or fails validation."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Provenance:
    """Where a fixture came from and how it was produced."""

    git_commit: str
    git_dirty: bool
    source_command: str
    environment: dict[str, Any]
    source_artifacts: tuple[str, ...] = ()
    exported_at: str | None = None


@dataclass(frozen=True)
class Fixture:
    """A validated measurement fixture."""

    kind: str
    schema_version: int
    generated_at: datetime
    provenance: Provenance
    limitations: tuple[str, ...]
    payload: dict[str, Any]
    path: Path = field(repr=False)


@dataclass(frozen=True)
class EndpointSample:
    """Per-endpoint counters from the live metrics snapshot."""

    method: str
    path: str
    status_counts: dict[str, int]
    p95_ms: float | None

    @property
    def requests(self) -> int:
        """Total observed requests for this endpoint."""
        return sum(self.status_counts.values())


@dataclass(frozen=True)
class LiveMetrics:
    """A validated ``GET /api/v1/metrics`` snapshot."""

    generated_at: datetime
    version: str
    environment: str
    ready: bool
    uptime_seconds: float
    totals: dict[str, int]
    chat_streams: dict[str, int]
    ingestion_jobs: dict[str, int]
    latency_ms: dict[str, dict[str, float | None]]
    endpoints: tuple[EndpointSample, ...]
    counters_disabled: bool = False


# ---------------------------------------------------------------------------
# Live-request bookkeeping (no secrets, ever)
# ---------------------------------------------------------------------------
_last_status: int | None = None
_last_error: str | None = None


def last_status() -> int | None:
    """HTTP status of the most recent :func:`load_live_metrics` call, if any."""
    return _last_status


def last_error() -> str | None:
    """Human-readable reason the last live fetch failed, if it did.

    The message never contains the API key, request headers or response body.
    """
    return _last_error


def _auth_headers() -> dict[str, str]:
    """Return the auth header when a key is configured, else an empty mapping.

    The value is only ever placed in the outgoing header — it is never logged,
    formatted into an error message or returned to a caller.
    """
    key = os.getenv(API_KEY_ENV_VAR, "")
    return {"X-API-Key": key} if key else {}


def _record_failure(status: int | None, message: str) -> None:
    global _last_status, _last_error
    _last_status = status
    _last_error = message


# ---------------------------------------------------------------------------
# Fixture loading and validation
# ---------------------------------------------------------------------------
def fixture_directory() -> Path:
    """Directory the fixtures are read from (overridable for deployments)."""
    override = os.getenv("DOCMIND_FIXTURES_DIR", "").strip()
    return Path(override) if override else FIXTURES_DIR


def _get(data: Any, path: tuple[str, ...], where: str) -> Any:
    """Return ``data[path[0]][path[1]]...`` or raise a precise error."""
    current = data
    traversed: list[str] = []
    for key in path:
        if not isinstance(current, dict):
            label = ".".join(traversed) or "<root>"
            raise FixtureError(f"{where}: '{label}' must be a JSON object")
        if key not in current:
            field_name = ".".join([*traversed, key])
            raise FixtureError(f"{where}: required field '{field_name}' is missing")
        current = current[key]
        traversed.append(key)
    return current


def _require_str(value: Any, name: str, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FixtureError(f"{where}: field '{name}' must be a string")
    if not allow_empty and not value.strip():
        raise FixtureError(f"{where}: field '{name}' must not be empty")
    return value


def _require_bool(value: Any, name: str, where: str) -> bool:
    if not isinstance(value, bool):
        raise FixtureError(f"{where}: field '{name}' must be a boolean")
    return value


def _require_int(value: Any, name: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixtureError(f"{where}: field '{name}' must be an integer")
    return value


def _require_number(value: Any, name: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureError(f"{where}: field '{name}' must be a number")
    return float(value)


def parse_timestamp(value: str, where: str) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting to UTC when no offset is given."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FixtureError(f"{where}: '{value}' is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_provenance(raw: Any, where: str) -> Provenance:
    git_commit = _require_str(_get(raw, ("git_commit",), where), "provenance.git_commit", where)
    git_dirty = _require_bool(_get(raw, ("git_dirty",), where), "provenance.git_dirty", where)
    source_command = _require_str(
        _get(raw, ("source_command",), where), "provenance.source_command", where
    )
    environment = _get(raw, ("environment",), where)
    if not isinstance(environment, dict):
        raise FixtureError(f"{where}: field 'provenance.environment' must be a JSON object")
    for key in ("python", "platform", "processor"):
        _require_str(
            _get(environment, (key,), f"{where}:provenance.environment"),
            f"provenance.environment.{key}",
            where,
        )
    cpu_count = _require_int(
        _get(environment, ("cpu_count",), f"{where}:provenance.environment"),
        "provenance.environment.cpu_count",
        where,
    )
    if cpu_count <= 0:
        raise FixtureError(f"{where}: field 'provenance.environment.cpu_count' must be positive")

    artifacts = raw.get("source_artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(item, str) for item in artifacts):
        raise FixtureError(
            f"{where}: field 'provenance.source_artifacts' must be a list of strings"
        )

    exported_at = raw.get("exported_at")
    if exported_at is not None and not isinstance(exported_at, str):
        raise FixtureError(f"{where}: field 'provenance.exported_at' must be a string")

    return Provenance(
        git_commit=git_commit,
        git_dirty=git_dirty,
        source_command=source_command,
        environment=dict(environment),
        source_artifacts=tuple(artifacts),
        exported_at=exported_at,
    )


def _parse_fixture(raw: Any, kind: str, path: Path) -> Fixture:
    where = f"fixture '{path.name}'"
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: top-level JSON value must be an object")

    schema_version = _require_int(_get(raw, ("schema_version",), where), "schema_version", where)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        known = ", ".join(str(item) for item in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise FixtureError(f"{where}: unsupported schema_version {schema_version} (known: {known})")

    declared_kind = _require_str(_get(raw, ("kind",), where), "kind", where)
    if declared_kind != kind:
        raise FixtureError(f"{where}: expected kind '{kind}', found '{declared_kind}'")

    generated_at = parse_timestamp(
        _require_str(_get(raw, ("generated_at",), where), "generated_at", where),
        f"{where}: generated_at",
    )

    limitations_raw = _get(raw, ("limitations",), where)
    if not isinstance(limitations_raw, list) or not limitations_raw:
        raise FixtureError(f"{where}: field 'limitations' must be a non-empty list")
    limitations: list[str] = []
    for index, item in enumerate(limitations_raw):
        limitations.append(_require_str(item, f"limitations[{index}]", where))

    payload = _get(raw, ("payload",), where)
    if not isinstance(payload, dict):
        raise FixtureError(f"{where}: field 'payload' must be a JSON object")

    return Fixture(
        kind=kind,
        schema_version=schema_version,
        generated_at=generated_at,
        provenance=_parse_provenance(_get(raw, ("provenance",), where), where),
        limitations=tuple(limitations),
        payload=payload,
        path=path,
    )


def load_fixture(kind: str, *, directory: Path | None = None) -> Fixture:
    """Load and validate one fixture.

    Args:
        kind: One of the keys of :data:`FIXTURE_FILES`.
        directory: Optional override for :func:`fixture_directory`.

    Raises:
        FixtureError: The file is missing, unreadable, not JSON, or fails
            validation (including a missing provenance field).
    """
    try:
        file_name = FIXTURE_FILES[kind]
    except KeyError:
        known = ", ".join(sorted(FIXTURE_FILES))
        raise FixtureError(f"Unknown fixture kind '{kind}' (known: {known})") from None

    path = (directory or fixture_directory()) / file_name
    if not path.is_file():
        raise FixtureError(
            f"Fixture '{kind}' is missing at {path}. "
            "Regenerate it with 'python -m demo.export_fixtures'."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FixtureError(
            f"Fixture '{kind}' at {path} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc
    except OSError as exc:
        raise FixtureError(f"Fixture '{kind}' at {path} could not be read: {exc.strerror}") from exc

    return _parse_fixture(raw, kind, path)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------
def age_days(generated_at: datetime, *, now: datetime | None = None) -> float:
    """Age of a measurement, in days (fractional)."""
    reference = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return (reference - generated_at).total_seconds() / 86400.0


def _configured_max_age_days() -> float:
    raw = os.getenv(MAX_AGE_ENV_VAR, "").strip()
    if not raw:
        return float(DEFAULT_MAX_AGE_DAYS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_MAX_AGE_DAYS)
    return value if value > 0 else float(DEFAULT_MAX_AGE_DAYS)


def is_stale(
    fixture: Fixture,
    max_age_days: float | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the fixture is older than the allowed age.

    The threshold defaults to :data:`DEFAULT_MAX_AGE_DAYS` and can be
    overridden through ``DOCMIND_FIXTURE_MAX_AGE_DAYS``.
    """
    limit = _configured_max_age_days() if max_age_days is None else float(max_age_days)
    return age_days(fixture.generated_at, now=now) > limit


# ---------------------------------------------------------------------------
# Formatting helpers (pure, shared by the page and its tests)
# ---------------------------------------------------------------------------
def format_ms(value: float | None) -> str:
    """Format a millisecond duration for a metric card."""
    if value is None:
        return "n/a"
    magnitude = abs(value)
    if magnitude < 1:
        return f"{value:.3f} ms"
    if magnitude < 100:
        return f"{value:.2f} ms"
    return f"{value:.1f} ms"


def format_uptime(seconds: float | None) -> str:
    """Format an uptime in seconds as a compact ``1h 02m`` style string."""
    if seconds is None or seconds < 0:
        return "n/a"
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_percent(value: float | None, *, digits: int = 1) -> str:
    """Format a 0..1 ratio as a percentage (``1.0`` -> ``100.0%``)."""
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------
def _dict_of_ints(value: Any, name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    result: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{name}.{key} must be a number")
        result[str(key)] = int(item)
    return result


def _latency_map(value: Any) -> dict[str, dict[str, float | None]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("latency_ms must be an object")
    result: dict[str, dict[str, float | None]] = {}
    for name, summary in value.items():
        if not isinstance(summary, dict):
            raise ValueError(f"latency_ms.{name} must be an object")
        result[str(name)] = {
            key: (None if summary.get(key) is None else float(summary[key]))
            for key in ("mean", "p50", "p95", "p99", "max")
        }
    return result


def _endpoints(value: Any) -> tuple[EndpointSample, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("endpoints must be a list")
    samples: list[EndpointSample] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each endpoint must be an object")
        method = item.get("method")
        path = item.get("path")
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError("each endpoint needs a string 'method' and 'path'")
        latency = item.get("latency_ms") or {}
        if not isinstance(latency, dict):
            raise ValueError("endpoint latency_ms must be an object")
        p95 = latency.get("p95")
        samples.append(
            EndpointSample(
                method=method,
                path=path,
                status_counts=_dict_of_ints(item.get("status_counts"), "status_counts"),
                p95_ms=None if p95 is None else float(p95),
            )
        )
    return tuple(sorted(samples, key=lambda sample: (sample.path, sample.method)))


def _parse_live(payload: Any) -> LiveMetrics:
    if not isinstance(payload, dict):
        raise ValueError("response body is not an object")

    generated_at_raw = payload.get("generated_at")
    if not isinstance(generated_at_raw, str):
        raise ValueError("generated_at must be an ISO-8601 string")
    generated_at = parse_timestamp(generated_at_raw, "live.generated_at")

    version = payload.get("version")
    if not isinstance(version, str):
        raise ValueError("version must be a string")
    environment = payload.get("environment")
    if not isinstance(environment, str):
        raise ValueError("environment must be a string")
    ready = payload.get("ready")
    if not isinstance(ready, bool):
        raise ValueError("ready must be a boolean")
    uptime = payload.get("uptime_seconds")
    if isinstance(uptime, bool) or not isinstance(uptime, (int, float)):
        raise ValueError("uptime_seconds must be a number")

    totals = _dict_of_ints(payload.get("totals"), "totals")
    uptime_seconds = float(uptime)
    counters_disabled = (
        totals.get("http_requests_total", 0) == 0
        and uptime_seconds > COUNTERS_DISABLED_AFTER_SECONDS
    )

    return LiveMetrics(
        generated_at=generated_at,
        version=version,
        environment=environment,
        ready=ready,
        uptime_seconds=uptime_seconds,
        totals=totals,
        chat_streams=_dict_of_ints(payload.get("chat_streams"), "chat_streams"),
        ingestion_jobs=_dict_of_ints(payload.get("ingestion_jobs"), "ingestion_jobs"),
        latency_ms=_latency_map(payload.get("latency_ms")),
        endpoints=_endpoints(payload.get("endpoints")),
        counters_disabled=counters_disabled,
    )


def _requests_module() -> Any:
    """Return the ``requests`` module, or ``None`` when the demo extra is absent.

    ``requests`` ships with the optional ``[demo]`` extra, but this module is
    imported by pytest and by the fixture exporter, which run without that
    extra. The import stays lazy so the data layer is importable everywhere.
    """
    try:
        import requests
    except ModuleNotFoundError:  # pragma: no cover - depends on the installation
        return None
    return requests


def load_live_metrics(timeout: float = REQUEST_TIMEOUT) -> LiveMetrics | None:
    """Fetch ``GET {API_BASE_URL}/metrics`` and return a validated snapshot.

    The endpoint reads the in-process Prometheus registry only, so a failed
    call costs nothing and a successful one spends no provider tokens.

    Returns:
        A :class:`LiveMetrics` snapshot, or ``None`` when the request is
        rejected (401/403), times out, cannot connect, or the payload does not
        match the expected schema. Exceptions are never propagated; the reason
        is available through :func:`last_error` / :func:`last_status`.
    """
    global _last_status, _last_error
    _record_failure(None, "")

    requests_module = _requests_module()
    if requests_module is None:
        _record_failure(
            None,
            "The optional 'requests' package is not installed, so live counters are "
            "unavailable. Install the demo extra: pip install -e '.[demo]'.",
        )
        return None

    url = f"{API_BASE_URL}{METRICS_PATH}"
    # Resolved through getattr so mypy does not require the optional dependency.
    timeout_error = getattr(requests_module, "Timeout", OSError)
    request_error = getattr(requests_module, "RequestException", OSError)
    try:
        response = requests_module.get(url, headers=_auth_headers(), timeout=timeout)
    except timeout_error:
        _record_failure(None, f"Timed out after {timeout:g}s while calling {url}.")
        return None
    except request_error as exc:
        # Only the exception class is surfaced: the exception text of some
        # transports can echo request details, and request details include the
        # auth header value.
        _record_failure(None, f"Could not reach {url} ({type(exc).__name__}).")
        return None

    status = getattr(response, "status_code", None)
    if status in (401, 403):
        _record_failure(
            int(status),
            f"{url} rejected the request with HTTP {status}. "
            "Set DOCMIND_API_KEY for this deployment to see live counters.",
        )
        return None
    if status != 200:
        _record_failure(
            None if status is None else int(status),
            f"{url} returned HTTP {status}.",
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        _record_failure(int(status), f"{url} did not return a JSON body.")
        return None

    try:
        snapshot = _parse_live(payload)
    except (FixtureError, KeyError, TypeError, ValueError) as exc:
        _record_failure(
            int(status),
            f"{url} returned an unexpected schema ({type(exc).__name__}: {exc}).",
        )
        return None

    _last_status = int(status)
    _last_error = None
    return snapshot
