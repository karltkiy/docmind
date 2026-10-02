"""Smoke tests for the FastAPI application wiring."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from src.main import app


async def test_root_endpoint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["name"]


async def test_health_endpoint_reports_dependencies() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body) == {
        "status",
        "database",
        "redis",
        "version",
        "environment",
        "provider_ready",
    }
    assert body["status"] in {"healthy", "degraded"}
    assert body["environment"] == "development"
    assert isinstance(body["provider_ready"], bool)


def test_openapi_paths_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/documents/upload" in paths
    assert "/api/v1/documents/{document_id}/status" in paths
    assert "/api/v1/chat/completions" in paths


async def test_upload_rejects_unsupported_type() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("malware.exe", b"binary", "application/octet-stream")},
        )
    assert response.status_code == 415
