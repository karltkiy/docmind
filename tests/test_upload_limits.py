"""Tests for upload size enforcement before full buffering."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app


async def test_upload_rejects_oversized_payload(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    payload = b"x" * (2 * 1024 * 1024)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("big.txt", payload, "text/plain")},
        )
    assert response.status_code == 413


async def test_upload_rejects_empty_file() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
    assert response.status_code == 400
