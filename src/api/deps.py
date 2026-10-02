"""Shared FastAPI dependencies: API-key authentication and rate limiting."""

from __future__ import annotations

import secrets
import time
from collections import deque
from threading import Lock

from fastapi import HTTPException, Request, status

from ..config import settings

_MAX_TRACKED_KEYS = 10_000


def client_id(request: Request) -> str:
    """Return a stable identifier for the calling client."""
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


async def require_api_key(request: Request) -> None:
    """Reject requests without a valid API key when authentication is enabled.

    When :attr:`Settings.API_KEY` is unset the dependency is a no-op, which
    keeps local development frictionless; production refuses to boot without a
    key (see ``Settings._validate_production_posture``).
    """
    configured = settings.API_KEY
    if not configured:
        return

    provided = request.headers.get(settings.API_KEY_HEADER)
    if not provided or not secrets.compare_digest(provided, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": settings.API_KEY_HEADER},
        )


class SlidingWindowLimiter:
    """Process-local sliding-window rate limiter.

    A single process cannot enforce a global limit across replicas; it still
    absorbs bursts and abuse cheaply. For a hard global limit, combine this with
    ``limit_req`` at the reverse proxy.
    """

    def __init__(self, window_seconds: int, limit: int) -> None:
        self._window = window_seconds
        self._limit = limit
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record a hit for ``key`` and report whether it is within budget."""
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(moment)
            if len(self._hits) > _MAX_TRACKED_KEYS:
                self._purge(cutoff)
            return True

    def _purge(self, cutoff: float) -> None:
        """Drop keys whose windows have fully expired."""
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            self._hits.pop(key, None)


_upload_limiter = SlidingWindowLimiter(
    settings.RATE_LIMIT_WINDOW_SECONDS, settings.UPLOAD_RATE_LIMIT_REQUESTS
)
_chat_limiter = SlidingWindowLimiter(
    settings.RATE_LIMIT_WINDOW_SECONDS, settings.CHAT_RATE_LIMIT_REQUESTS
)


def _enforce(limiter: SlidingWindowLimiter, request: Request) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    if limiter.allow(client_id(request)):
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Rate limit exceeded. Please retry later.",
        headers={"Retry-After": str(settings.RATE_LIMIT_WINDOW_SECONDS)},
    )


async def enforce_upload_rate_limit(request: Request) -> None:
    """Rate-limit document uploads."""
    _enforce(_upload_limiter, request)


async def enforce_chat_rate_limit(request: Request) -> None:
    """Rate-limit chat completions."""
    _enforce(_chat_limiter, request)
