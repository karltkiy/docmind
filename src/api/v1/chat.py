"""Retrieval-augmented chat with Server-Sent Event token streaming."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ...db.base import SessionLocal
from ...db.models import Document
from ...schemas.chat import ChatRequest
from ...services.rag_engine import rag_engine
from ...services.vector_store import SearchResult, VectorStore

logger = logging.getLogger(__name__)

router = APIRouter()

_PROMPT_TEMPLATE = (
    "You are DocMind, a helpful assistant. Answer the user's question using only "
    "the provided context. If the answer is not in the context, say you don't know.\n\n"
    "Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _load_filenames(document_ids: set) -> dict:
    if not document_ids:
        return {}
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Document.id, Document.filename).where(Document.id.in_(document_ids))
        )
        return {row.id: row.filename for row in rows}


def _serialize_sources(
    results: list[SearchResult], filenames: dict
) -> list[dict]:
    sources = []
    for result in results:
        chunk = result.chunk
        sources.append(
            {
                "document_id": str(chunk.document_id),
                "filename": filenames.get(chunk.document_id),
                "chunk_index": chunk.chunk_index,
                "score": round(result.score, 4),
                "excerpt": chunk.content[:300],
            }
        )
    return sources


@router.post("/completions")
async def chat_completion(request: ChatRequest) -> StreamingResponse:
    """Stream a grounded answer as Server-Sent Events."""

    async def event_generator() -> AsyncIterator[str]:
        try:
            embedding = await rag_engine.get_embedding(request.query)

            async with SessionLocal() as session:
                results = await VectorStore.search_chunks(
                    session=session,
                    query_embedding=embedding,
                    top_k=request.top_k,
                    document_ids=request.document_ids,
                )

            filenames = await _load_filenames(
                {result.chunk.document_id for result in results}
            )
            yield _sse(
                {"type": "sources", "content": _serialize_sources(results, filenames)}
            )

            context = "\n\n".join(
                f"[{index + 1}] {result.chunk.content}"
                for index, result in enumerate(results)
            ) or "No relevant context was found."
            prompt = _PROMPT_TEMPLATE.format(context=context, question=request.query)

            async for token in rag_engine.generate_answer_stream(prompt):
                yield _sse({"type": "token", "content": token})

            yield _sse({"type": "done"})
        except Exception as exc:  # noqa: BLE001 - stream errors as SSE events
            logger.exception("Chat completion failed")
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
