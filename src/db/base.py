import sqlalchemy
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    AsyncEngine,
    create_async_engine
)
from sqlalchemy.orm import DeclarativeBase
from typing import Any
from .config import settings

# Construct the database URL
db_url = f"postgresql+asyncpg://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"

# Create the engine
engine: AsyncEngine = create_async_engine(
    db_url,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
)

# Create the session factory
SessionLocal = async_sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# Base class for models
class Base(DeclarativeBase):
    pass

# Dependency for FastAPI
async def get_db_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
