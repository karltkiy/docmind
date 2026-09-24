"""Async SQLAlchemy engine, session factory and FastAPI dependencies."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ..config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base class shared by all ORM models."""


engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session.

    The session is closed by the surrounding context manager and rolled back if
    the request handler raises, with the failure logged before propagation.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            logger.exception("Request-scoped database session rolled back.")
            raise


def get_session_factory() -> Callable[[], AsyncSession]:
    """Provide the session factory for streaming endpoints.

    Streaming responses outlive the request dependency lifecycle, so they need
    the factory itself (to open short-lived sessions) rather than a single
    request-scoped session.
    """
    return SessionLocal


async def dispose_engine() -> None:
    """Dispose of the connection pool on application shutdown."""
    await engine.dispose()
