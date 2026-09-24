"""Pluggable RAG engine supporting OpenAI (cloud) and Ollama (local).

The engine exposes a provider-agnostic API for embeddings and chat completion,
including true token streaming for Server-Sent Events. Transport clients are
created once during application ``startup()`` and reused for the process
lifetime, then released by ``shutdown()``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings

logger = logging.getLogger(__name__)

_OPENAI_TIMEOUT = 60.0
_OLLAMA_TIMEOUT = 120.0
_MAX_CONNECTIONS = 20
_MAX_KEEPALIVE_CONNECTIONS = 10

# Shared retry policy for transient network/provider failures. Reused across
# every provider call; ``reraise`` surfaces the original error after exhaustion.
_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider returns an unusable response."""


class RAGEngine:
    """Embeddings + chat completion facade over a configurable provider.

    The instance is a process-wide singleton; it must not be used before
    :meth:`startup` has been awaited.
    """

    def __init__(self) -> None:
        self.embedding_provider: str = settings.EMBEDDING_PROVIDER
        self.llm_provider: str = settings.LLM_PROVIDER
        self._openai: AsyncOpenAI | None = None
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def _uses_openai(self) -> bool:
        return "openai" in {self.embedding_provider, self.llm_provider}

    async def startup(self) -> None:
        """Create the reusable transport clients."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=settings.OLLAMA_BASE_URL,
                timeout=_OLLAMA_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=_MAX_CONNECTIONS,
                    max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
                ),
            )
            logger.debug("Initialised shared Ollama HTTP client.")
        if self._uses_openai and self._openai is None:
            self._openai = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                timeout=_OPENAI_TIMEOUT,
            )
            logger.debug("Initialised shared OpenAI client.")

    async def shutdown(self) -> None:
        """Release every transport client owned by the engine."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._openai is not None:
            await self._openai.close()
            self._openai = None

    @property
    def openai(self) -> AsyncOpenAI:
        """Return the shared OpenAI client, or fail if startup was skipped."""
        if self._openai is None:
            raise RuntimeError("RAGEngine.startup() must be awaited before use.")
        return self._openai

    @property
    def http(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, or fail if startup was skipped."""
        if self._http is None:
            raise RuntimeError("RAGEngine.startup() must be awaited before use.")
        return self._http

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------
    @_retry_transient
    async def _ollama_embeddings(self, texts: list[str]) -> list[list[float]]:
        response = await self.http.post(
            "/api/embed",
            json={"model": settings.embedding_model, "input": texts},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        embeddings = payload.get("embeddings")
        if not embeddings:
            raise EmbeddingError("Ollama returned no embeddings.")
        return embeddings

    @_retry_transient
    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for a batch of texts."""
        if not texts:
            return []
        if self.embedding_provider == "openai":
            response = await self.openai.embeddings.create(
                input=texts,
                model=settings.embedding_model,
            )
            return [item.embedding for item in response.data]
        return await self._ollama_embeddings(texts)

    async def get_embedding(self, text: str) -> list[float]:
        """Return the embedding for a single text.

        Raises:
            EmbeddingError: If the provider returns no vector for the input.
        """
        embeddings = await self.get_embeddings([text])
        if not embeddings:
            raise EmbeddingError("Embedding provider returned no vector.")
        return embeddings[0]

    # ------------------------------------------------------------------
    # Chat completion
    # ------------------------------------------------------------------
    @_retry_transient
    async def generate_answer(self, prompt: str) -> str:
        """Return a complete, non-streamed answer."""
        if self.llm_provider == "openai":
            response = await self.openai.chat.completions.create(
                model=settings.llm_model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""
        http_response = await self.http.post(
            "/api/generate",
            json={"model": settings.llm_model, "prompt": prompt, "stream": False},
        )
        http_response.raise_for_status()
        payload: dict[str, Any] = http_response.json()
        return str(payload.get("response", ""))

    async def generate_answer_stream(self, prompt: str) -> AsyncIterator[str]:
        """Yield answer tokens as they are produced."""
        if self.llm_provider == "openai":
            stream = await self.openai.chat.completions.create(
                model=settings.llm_model,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
            return

        async with self.http.stream(
            "POST",
            "/api/generate",
            json={"model": settings.llm_model, "prompt": prompt, "stream": True},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    data: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed Ollama stream line: %s", line)
                    continue
                if data.get("response"):
                    yield str(data["response"])
                if data.get("done"):
                    break


rag_engine = RAGEngine()
