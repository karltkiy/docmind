"""Aggregate router for API v1."""

from fastapi import APIRouter, Depends

from ..deps import require_api_key
from .chat import router as chat_router
from .documents import router as documents_router

api_router = APIRouter()
api_router.include_router(
    documents_router,
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(require_api_key)],
)
api_router.include_router(
    chat_router,
    prefix="/chat",
    tags=["chat"],
    dependencies=[Depends(require_api_key)],
)
