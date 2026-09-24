"""DocMind API application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api.v1.router import api_router
from .config import settings
from .db.base import dispose_engine, engine
from .worker.queue import close_redis_pool, get_redis_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise shared resources on startup and release them on shutdown."""
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    yield
    await close_redis_pool()
    await dispose_engine()


app = FastAPI(
    title=settings.APP_NAME,
    description="Enterprise RAG microservice",
    version="1.0.0",
    lifespan=lifespan,
)

_allow_all_origins = settings.cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=not _allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {"name": settings.APP_NAME, "version": "1.0.0", "docs": "/docs"}


@app.get("/health", tags=["meta"])
async def health_check() -> JSONResponse:
    """Report liveness plus downstream dependency health."""
    database_ok = True
    redis_ok = True

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health probe reports, does not raise
        database_ok = False

    try:
        pool = await get_redis_pool()
        await pool.ping()
    except Exception:  # noqa: BLE001 - health probe reports, does not raise
        redis_ok = False

    healthy = database_ok and redis_ok
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "degraded",
            "database": database_ok,
            "redis": redis_ok,
        },
    )
