"""Retrieval-augmented chat with Server-Sent Event token streaming."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.base import get_session_factory
from ...db.models import Document
from ...schemas.chat import ChatRequest, ChatSource, SsePayload
from ...services.rag_engine import rag_engine
from ...services.vector_store import SearchResult, VectorStore

logger = logging.getLogger(__name__)

router = APIRouter()

SessionFactory = Callable[[], AsyncSession]

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


def _sse(payload: SsePayload) -> str:
    """Encode an SSE frame from a typed payload."""
    return f"data: {json.dumps(payload)}\n\n"


async def _load_filenames(
    session: AsyncSession, document_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Resolve document ids to their original filenames."""
    if not document_ids:
        return {}
    rows = await session.execute(
        select(Document.id, Document.filename).where(Document.id.in_(document_ids))
    )
    return {row.id: row.filename for row in rows}


def _serialize_sources(
    results: list[SearchResult], filenames: Mapping[uuid.UUID, str]
) -> list[ChatSource]:
    """Convert retrieval hits into API-facing source models."""
    return [
        ChatSource(
            document_id=result.chunk.document_id,
            filename=filenames.get(result.chunk.document_id),
            chunk_index=result.chunk.chunk_index,
            score=round(result.score, 4),
            excerpt=result.chunk.content[:300],
        )
        for result in results
    ]


@router.post(
    "/completions",
    summary="Stream a grounded answer",
    description=(
        "Retrieve the most relevant chunks for the query and stream a grounded "
        "answer as Server-Sent Events. Frames are one of `sources`, `token`, "
        "`done`, or `error`."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "SSE stream of `sources`, `token`, `done`/`error` frames.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def chat_completion(
    request: ChatRequest,
    http_request: Request,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> StreamingResponse:
    """Stream a grounded answer as Server-Sent Events."""
    request_id = getattr(http_request.state, "request_id", "unknown")

    async def event_generator() -> AsyncIterator[str]:
        try:
            embedding = await rag_engine.get_embedding(request.query)

            async with session_factory() as session:
                results = await VectorStore.search_chunks(
                    session=session,
                    query_embedding=embedding,
                    top_k=request.top_k,
                    document_ids=request.document_ids,
                )
                filenames = await _load_filenames(
                    session, {result.chunk.document_id for result in results}
                )

            sources = _serialize_sources(results, filenames)
            yield _sse(
                {
                    "type": "sources",
                    "content": [source.model_dump(mode="json") for source in sources],
                }
            )

            context = "\n\n".join(
                f"[{index + 1}] {result.chunk.content}"
                for index, result in enumerate(results)
            ) or "No relevant context was found."
            prompt = _PROMPT_TEMPLATE.format(context=context, question=request.query)

            async for token in rag_engine.generate_answer_stream(prompt):
                yield _sse({"type": "token", "content": token})

            yield _sse({"type": "done"})
        except Exception:
            # Never leak internal exception details to the client; correlate the
            # opaque message with the server-side traceback via ``request_id``.
            logger.exception("Chat completion failed (request_id=%s)", request_id)
            yield _sse(
                {
                    "type": "error",
                    "message": "The answer could not be generated.",
                    "request_id": request_id,
                }
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
