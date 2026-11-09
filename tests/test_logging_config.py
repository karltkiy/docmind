"""Unit tests for the structured logging formatters.

No handlers, files or application settings are involved: the records are built
by hand and rendered directly.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from src.logging_config import ConsoleFormatter, JsonFormatter

STANDARD_ATTRIBUTES = {
    "msg",
    "args",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "name",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "asctime",
}


def _record(message: str = "Retrieval completed.", **extra: Any) -> logging.LogRecord:
    record = logging.LogRecord(
        name="docmind.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_keeps_structured_extras() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record(
                retrieval_ms=12.34,
                ingestion_ms=98.5,
                document_id="0f7c2b6e-0000-4000-8000-000000000000",
                request_id="req-123",
            )
        )
    )

    assert payload["retrieval_ms"] == 12.34
    assert payload["ingestion_ms"] == 98.5
    assert payload["document_id"] == "0f7c2b6e-0000-4000-8000-000000000000"
    assert payload["request_id"] == "req-123"
    assert payload["message"] == "Retrieval completed."
    assert payload["level"] == "INFO"
    assert payload["logger"] == "docmind.test"


def test_json_formatter_omits_standard_record_attributes() -> None:
    payload = json.loads(JsonFormatter().format(_record(value=1)))

    leaked = STANDARD_ATTRIBUTES & payload.keys()
    assert leaked == set()
    # ``message`` is the formatter's own rendered field, not a raw record key.
    assert payload["message"] == "Retrieval completed."
    assert payload["value"] == 1


def test_json_formatter_survives_unserialisable_extras() -> None:
    marker = object()

    payload = json.loads(JsonFormatter().format(_record(payload=marker)))

    assert payload["payload"] == str(marker)


def test_json_formatter_ignores_private_extras() -> None:
    payload = json.loads(JsonFormatter().format(_record(_secret_context="ignore me")))

    assert "_secret_context" not in payload


def test_json_formatter_includes_exception_and_stack_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()

    record = _record()
    record.exc_info = exc_info
    record.stack_info = "Stack (most recent call last):"

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]
    assert payload["stack"] == "Stack (most recent call last):"


def test_console_formatter_appends_request_id_and_scalar_extras() -> None:
    line = ConsoleFormatter().format(
        _record(retrieval_ms=12.3, cache_hit=True, request_id="req-123", nested={"a": 1})
    )

    assert "[request_id=req-123]" in line
    assert "[retrieval_ms=12.3]" in line
    assert "[cache_hit=True]" in line
    assert "nested" not in line
    assert line.index("[request_id=") < line.index("[retrieval_ms=")


def test_console_formatter_without_extras_keeps_the_plain_line() -> None:
    line = ConsoleFormatter().format(_record())

    assert line.endswith("docmind.test: Retrieval completed.")
    assert "[" not in line
