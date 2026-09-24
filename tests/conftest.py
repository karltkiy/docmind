"""Shared pytest fixtures and test environment configuration."""

from __future__ import annotations

import os

# Configure the environment before any application module is imported so that
# the cached Settings instance picks up test-friendly values.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/docmind_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "openai")
os.environ.setdefault("DEBUG", "false")

import pytest  # noqa: E402


@pytest.fixture()
def sample_text() -> str:
    return " ".join(f"word{i}" for i in range(1200))
