"""Structured logging setup shared by the API process and the Arq worker.

The standard library is used deliberately: a single ``StreamHandler`` writing
either single-line JSON (production, container-friendly) or a compact console
format (local development). Records emitted with a ``request_id`` extra are
enriched automatically so API logs can be correlated end to end.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Loggers owned by third-party servers that we route through the root handler so
# every line shares one formatter and one destination.
_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "arq",
    "arq.worker",
)


class JsonFormatter(logging.Formatter):
    """Render :class:`logging.LogRecord` objects as single-line JSON documents."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact, human-readable formatter for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install a single root handler and align third-party loggers with it.

    Args:
        level: Case-insensitive log level name; unknown values fall back to
            ``INFO``.
        json_output: Emit JSON when ``True``, otherwise the console format.
    """
    resolved_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(resolved_level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True
        third_party.setLevel(resolved_level)
