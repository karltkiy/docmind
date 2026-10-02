"""Tests for API-key authentication and the process-local rate limiter."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.deps import SlidingWindowLimiter, require_api_key
from src.config import settings


def _request(headers: dict[str, str]) -> Request:
    raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/documents",
            "headers": raw,
            "client": ("1.2.3.4", 12345),
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )


async def test_api_key_required_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "API_KEY", "top-secret")
    with pytest.raises(HTTPException) as excinfo:
        await require_api_key(_request({}))
    assert excinfo.value.status_code == 401
    assert excinfo.value.headers == {"WWW-Authenticate": settings.API_KEY_HEADER}


async def test_api_key_rejects_wrong_value(monkeypatch) -> None:
    monkeypatch.setattr(settings, "API_KEY", "top-secret")
    with pytest.raises(HTTPException) as excinfo:
        await require_api_key(_request({settings.API_KEY_HEADER: "nope"}))
    assert excinfo.value.status_code == 401


async def test_api_key_accepts_valid_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "API_KEY", "top-secret")
    # No exception means the dependency is satisfied.
    await require_api_key(_request({settings.API_KEY_HEADER: "top-secret"}))


async def test_auth_disabled_without_key() -> None:
    await require_api_key(_request({}))


def test_sliding_window_limiter_enforces_budget() -> None:
    limiter = SlidingWindowLimiter(window_seconds=60, limit=2)
    assert limiter.allow("client", now=0.0) is True
    assert limiter.allow("client", now=0.1) is True
    assert limiter.allow("client", now=0.2) is False
    # A new window frees the budget.
    assert limiter.allow("client", now=61.0) is True


def test_sliding_window_limiter_is_per_key() -> None:
    limiter = SlidingWindowLimiter(window_seconds=60, limit=1)
    assert limiter.allow("a", now=0.0) is True
    assert limiter.allow("b", now=0.0) is True
    assert limiter.allow("a", now=0.1) is False
