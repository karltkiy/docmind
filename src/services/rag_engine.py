import httpx
from typing import List, Optional
from openai import AsyncOpenAI
from .config import settings

class RAGEngine:
    def __init__(self):
        self.embedding_provider = settings.EMBEDDING_PROVIDER.lower()
        self.llm_provider = settings.LLM_PROVIDER.lower()
        
        if self.embedding_provider == "openai":
            self.embedding_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        else:
            self.embedding_client = None # Ollama handled via httpx

        if self.llm_provider == "openai":
            self.llm_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        else:
            self.llm_client = None # Ollama handled via httpx

    async def get_embedding(self, text: str) -> List[float]:
        if self.embedding_provider == "openai":
            response = await self.embedding_client.embeddings.create(
                input=text,
                model="text-embedding-3-small"
            )
            return response.data[0].embedding
        else:
            # Ollama implementation
            async with httpx.AsyncClient(base_url=settings.OLLAMA_BASE_URL) as client:
                response = await client.post(
                    "/api/embeddings",
                    json={"model": "nomic-embed-text", "prompt": text}
                )
                response.raise_for_status()
                return response.json()["embedding"]

    async def generate_answer(self, prompt: str) -> str:
        if self.llm_provider == "openai":
            response = await self.llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        else:
            # Ollama implementation
            async with httpx.AsyncClient(base_url=settings.OLLAMA_BASE_URL) as client:
                response = await client.post(
                    "/api/generate",
                    json={
                        "model": "llama3.1:8b",
                        "prompt": prompt,
                        "stream": False
                    }
                )
                response.raise_for_status()
                return response.json()["response"]

    async def generate_answer_stream(self, prompt: str):
        if self.llm_provider == "openai":
            response = await self.llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=True
            )
            for chunk in response._stream_generator():
                yield chunk.choices[0].delta.content
        else:
            # Ollama implementation
            async with httpx.AsyncClient(base_url=settings.OLLAMA_BASE_URL) as client:
                response = await client.post(
                    "/api/generate",
                    json={
                        "model": "llama3.1:8b",
                        "prompt": prompt,
                        "stream": True
                    }
                )
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        # Ollama's streaming response is a bit different, it's usually JSON objects
                        # but for simplicity in this implementation, we'll just yield the raw line
                        # if it's a valid JSON.
                        # Actually, for Ollama, the response is a stream of JSON objects.
                        # We'll just yield the content if it's a valid JSON.
                        import json
                        try:
                            data = json.loads(line.decode('utf-8'))
                            yield data.get("response", "")
                        except:
                            pass

# Singleton instance
rag_engine = RAGEngine()
