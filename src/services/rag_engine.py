"""Pluggable RAG engine supporting OpenAI (cloud) and Ollama (local).

The engine exposes a provider-agnostic API for embeddings and chat completion,
including true token streaming for Server-Sent Events.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings

logger = logging.getLogger(__name__)

_OPENAI_TIMEOUT = 60.0
_OLLAMA_TIMEOUT = 120.0

# Shared retry policy for transient network/provider failures.
_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)


class RAGEngine:
    """Embeddings + chat completion facade over a configurable provider."""

    def __init__(self) -> None:
        self.embedding_provider = settings.EMBEDDING_PROVIDER
        self.llm_provider = settings.LLM_PROVIDER
        self._openai: AsyncOpenAI | None = None

        if "openai" in {self.embedding_provider, self.llm_provider}:
            if not settings.OPENAI_API_KEY:
                logger.warning(
                    "OPENAI_API_KEY is not configured; OpenAI requests will fail."
                )
            self._openai = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY or "missing-api-key",
                timeout=_OPENAI_TIMEOUT,
            )

    @property
    def openai(self) -> AsyncOpenAI:
        if self._openai is None:
            raise RuntimeError("OpenAI client is not initialised for this provider.")
        return self._openai

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------
    @retry(**_RETRY)
    async def _ollama_embeddings(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(
            base_url=settings.OLLAMA_BASE_URL, timeout=_OLLAMA_TIMEOUT
        ) as client:
            response = await client.post(
                "/api/embed",
                json={"model": settings.embedding_model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()
        embeddings = payload.get("embeddings")
        if not embeddings:
            # Fallback for older Ollama versions exposing a singular endpoint.
            raise ValueError("Ollama returned no embeddings.")
        return embeddings

    @retry(**_RETRY)
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
        """Return the embedding for a single text."""
        embeddings = await self.get_embeddings([text])
        return embeddings[0]

    # ------------------------------------------------------------------
    # Chat completion
    # ------------------------------------------------------------------
    @retry(**_RETRY)
    async def generate_answer(self, prompt: str) -> str:
        """Return a complete, non-streamed answer."""
        if self.llm_provider == "openai":
            response = await self.openai.chat.completions.create(
                model=settings.llm_model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""
        async with httpx.AsyncClient(
            base_url=settings.OLLAMA_BASE_URL, timeout=_OLLAMA_TIMEOUT
        ) as client:
            response = await client.post(
                "/api/generate",
                json={"model": settings.llm_model, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            return response.json().get("response", "")

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

        async with httpx.AsyncClient(
            base_url=settings.OLLAMA_BASE_URL, timeout=_OLLAMA_TIMEOUT
        ) as client:
            async with client.stream(
                "POST",
                "/api/generate",
                json={"model": settings.llm_model, "prompt": prompt, "stream": True},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed Ollama stream line: %s", line)
                        continue
                    if data.get("response"):
                        yield data["response"]
                    if data.get("done"):
                        break


rag_engine = RAGEngine()
