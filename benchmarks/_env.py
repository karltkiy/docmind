"""Safe environment bootstrap shared by the benchmark scripts.

Importing this module has no side effects. Call :func:`configure_environment`
before importing application modules, and import them inside
:func:`isolated_cwd` so pydantic-settings cannot pick up a real ``.env`` file.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Dummy, non-secret values. They are *forced* (not setdefault) so a benchmark
# run never validates against real credentials and never connects anywhere.
BENCH_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/docmind_bench",
    "REDIS_URL": "redis://localhost:6379/0",
    "OPENAI_API_KEY": "bench-key-not-used",
    "EMBEDDING_PROVIDER": "openai",
    "LLM_PROVIDER": "openai",
    "ENVIRONMENT": "development",
    "DEBUG": "false",
    "METRICS_ENABLED": "true",
    "LOG_LEVEL": "WARNING",
    "LOG_JSON": "false",
}


def configure_environment() -> None:
    """Force the benchmark-only environment variables into ``os.environ``."""
    os.environ.update(BENCH_ENV)


@contextmanager
def isolated_cwd() -> Iterator[None]:
    """Temporarily run from an empty directory so no real ``.env`` is read."""
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="docmind-bench-") as temporary:
        os.chdir(temporary)
        try:
            yield
        finally:
            os.chdir(previous)
