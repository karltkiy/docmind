"""DocMind API application entrypoint."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api.v1.router import api_router
from .config import settings
from .db.base import dispose_engine, engine
from .logging_config import configure_logging
from .schemas.meta import HealthResponse, RootResponse
from .services.rag_engine import rag_engine
from .worker.queue import close_redis_pool, get_redis_pool

configure_logging(settings.LOG_LEVEL, json_output=settings.LOG_JSON)

logger = logging.getLogger(__name__)

_APP_VERSION = "1.0.0"
_REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise shared resources on startup and release them on shutdown."""
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    await rag_engine.startup()
    logger.info("DocMind API started (debug=%s).", settings.DEBUG)
    try:
        yield
    finally:
        await rag_engine.shutdown()
        await close_redis_pool()
        await dispose_engine()
        logger.info("DocMind API shut down cleanly.")


app = FastAPI(
    title=settings.APP_NAME,
    description="Enterprise RAG microservice",
    version=_APP_VERSION,
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


@app.middleware("http")
async def attach_request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach a correlation id to the request state and every response."""
    request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[_REQUEST_ID_HEADER] = request_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unexpected errors and return an opaque, correlated 500 payload."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception (request_id=%s, path=%s)", request_id, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error.", "request_id": request_id},
    )


app.include_router(api_router, prefix="/api/v1")


@app.get(
    "/",
    tags=["meta"],
    response_model=RootResponse,
    summary="Service metadata",
    description="Return the service name, version and documentation location.",
)
async def root() -> RootResponse:
    """Return basic service identity."""
    return RootResponse(name=settings.APP_NAME, version=_APP_VERSION, docs="/docs")


@app.get(
    "/health",
    tags=["meta"],
    response_model=HealthResponse,
    summary="Liveness and dependency health",
    description="Report liveness together with PostgreSQL and Redis reachability.",
    responses={503: {"description": "At least one dependency is unavailable."}},
)
async def health_check(response: Response) -> HealthResponse:
    """Report liveness plus downstream dependency health."""
    database_ok = True
    redis_ok = True

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health probe reports, does not raise
        database_ok = False
        logger.warning("Database health probe failed.", exc_info=True)

    try:
        pool = await get_redis_pool()
        await pool.ping()
    except Exception:  # noqa: BLE001 - health probe reports, does not raise
        redis_ok = False
        logger.warning("Redis health probe failed.", exc_info=True)

    healthy = database_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="healthy" if healthy else "degraded",
        database=database_ok,
        redis=redis_ok,
    )
