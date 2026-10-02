"""Integration tests against a real PostgreSQL + pgvector database.

These tests are skipped automatically when no database is reachable, so the
default suite stays fast and dependency-free. CI runs them in a dedicated job
with a ``pgvector`` service container.

Every test builds its own engine with ``NullPool``: pytest-asyncio runs each
async test in a fresh event loop, so a pooled connection created in one loop
must never be handed to another (asyncpg raises "attached to a different loop").
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.api.v1 import documents as documents_module
from src.config import settings
from src.db.base import get_db_session
from src.db.schema_guard import verify_embedding_dimension
from src.main import app

pytestmark = pytest.mark.integration


class _FakePool:
    """Minimal arq pool stand-in that accepts enqueued jobs."""

    async def enqueue_job(self, *args: object, **kwargs: object) -> None:
        return None


@pytest.fixture()
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Yield a loop-local, unpooled engine bound to a migrated database."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - absence of a database is a skip, not a failure
        await engine.dispose()
        pytest.skip("PostgreSQL is not reachable; skipping integration tests.")

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_embedding_dimension_guard_passes(db_engine: AsyncEngine) -> None:
    await verify_embedding_dimension(db_engine)


async def test_upload_list_delete_roundtrip(
    monkeypatch, tmp_path: Path, db_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def _fake_pool() -> _FakePool:
        return _FakePool()

    monkeypatch.setattr(documents_module, "get_redis_pool", _fake_pool)
    app.dependency_overrides[get_db_session] = _override_session
    try:
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
    finally:
        app.dependency_overrides.pop(get_db_session, None)

    assert not list(tmp_path.iterdir())
