"""Tests for the pure-ASGI metrics middleware and SSE pass-through."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from src.metrics import MetricsMiddleware, render_metrics, route_label


async def _asgi_call(app: Any, path: str) -> list[dict]:
    messages: list[dict] = []
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # StreamingResponse listens for client disconnects on a separate task;
        # yield control instead of busy-looping so the stream can complete and
        # the listener gets cancelled once the response is finished.
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("1.2.3.4", 12345),
        "server": ("test", 80),
        "scheme": "http",
    }
    await app(scope, receive, send)
    return messages


def test_route_label_collapses_uuids() -> None:
    assert route_label("/api/v1/documents/1f8f2c3a-1111-2222-3333-444455556666/status") == (
        "/api/v1/documents/{id}/status"
    )
    assert route_label("/health") == "/health"


def test_streaming_response_is_not_buffered() -> None:
    """Each SSE chunk must reach the client as its own body message."""
    app = FastAPI()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def generator():
            for index in range(3):
                yield f"data: {index}\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream")

    app.add_middleware(MetricsMiddleware)

    messages = asyncio.run(_asgi_call(app, "/stream"))
    bodies = [m for m in messages if m["type"] == "http.response.body"]

    # A buffering middleware would collapse the three frames into one message.
    assert len(bodies) >= 3
    payload = b"".join(m.get("body", b"") for m in bodies)
    assert payload == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_metrics_middleware_records_request() -> None:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(MetricsMiddleware)

    asyncio.run(_asgi_call(app, "/ping"))
    payload, _ = render_metrics()
    assert b"docmind_http_requests_total" in payload
    assert b'path="/ping"' in payload
