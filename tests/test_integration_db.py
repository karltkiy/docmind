"""Integration tests against a real PostgreSQL + pgvector database.

These tests are skipped automatically when no database is reachable, so the
default suite stays fast and dependency-free. CI runs them in a dedicated job
with a ``pgvector`` service container.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from src.api.v1 import documents as documents_module
from src.config import settings
from src.db.base import engine
from src.db.schema_guard import verify_embedding_dimension
from src.main import app

pytestmark = pytest.mark.integration


async def _database_available() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - absence of a database is a skip, not a failure
        return False


@pytest.fixture(scope="module")
async def migrated_database() -> None:
    """Ensure the schema exists before running the integration tests."""
    if not await _database_available():
        pytest.skip("PostgreSQL is not reachable; skipping integration tests.")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
    )


class _FakePool:
    """Minimal arq pool stand-in that accepts enqueued jobs."""

    async def enqueue_job(self, *args: object, **kwargs: object) -> None:
        return None


async def test_embedding_dimension_guard_passes(migrated_database: None) -> None:
    await verify_embedding_dimension(engine)


async def test_upload_list_delete_roundtrip(
    monkeypatch, tmp_path: Path, migrated_database: None
) -> None:
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    async def _fake_pool() -> _FakePool:
        return _FakePool()

    monkeypatch.setattr(documents_module, "get_redis_pool", _fake_pool)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("note.txt", b"hello docmind", "text/plain")},
        )
        assert upload.status_code == 202, upload.text
        document_id = upload.json()["id"]

        listing = await client.get("/api/v1/documents", params={"limit": 5, "offset": 0})
        assert listing.status_code == 200
        assert any(item["id"] == document_id for item in listing.json())

        status_response = await client.get(f"/api/v1/documents/{document_id}/status")
        assert status_response.status_code == 200
        assert status_response.json()["filename"] == "note.txt"

        deleted = await client.delete(f"/api/v1/documents/{document_id}")
        assert deleted.status_code == 204

    assert not list(tmp_path.iterdir())
