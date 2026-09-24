from fastapi import APIRouter
from src.api.v1.documents import router as documents_router
from src.api.v1.chat import router as chat_router

api_router = APIRouter()

api_router.include_router(documents_router, prefix="/documents", tags=["documents"])
api_router.include_router(chat_router, prefix="/chat", tags=["chat"])
