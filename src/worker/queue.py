"""Shared arq Redis connection pool used by the API to enqueue jobs."""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from ..config import settings

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    """Build arq Redis settings from the application configuration."""
    return RedisSettings.from_dsn(settings.redis_url)


async def get_redis_pool() -> ArqRedis:
    """Return a lazily-created, process-wide arq Redis pool."""
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_redis_pool() -> None:
    """Close the shared pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
