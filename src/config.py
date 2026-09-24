"""Centralised application configuration.

All settings are sourced from environment variables (or a local ``.env`` file)
and validated by Pydantic v2. Derived values such as the async database URL and
Redis DSN are exposed as computed fields so the rest of the codebase never has
to build connection strings by hand.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "ollama"]

_DEFAULT_EMBEDDING_MODELS: dict[str, str] = {
    "openai": "text-embedding-3-small",
    "ollama": "nomic-embed-text",
}
_DEFAULT_LLM_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1:8b",
}
_DEFAULT_EMBEDDING_DIMS: dict[str, int] = {
    "openai": 1536,
    "ollama": 768,
}


class Settings(BaseSettings):
    """Runtime configuration for the DocMind API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    APP_NAME: str = "DocMind API"
    DEBUG: bool = True
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "*"

    # PostgreSQL
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "docmind"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    # Explicit override; when unset it is derived from the POSTGRES_* values.
    DATABASE_URL: str | None = None

    # Redis / Arq
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_URL: str | None = None

    # RAG providers
    EMBEDDING_PROVIDER: Provider = "openai"
    LLM_PROVIDER: Provider = "openai"
    EMBEDDING_MODEL: str | None = None
    LLM_MODEL: str | None = None
    EMBEDDING_DIM: int | None = None
    RETRIEVAL_TOP_K: int = 4
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # Provider credentials
    OPENAI_API_KEY: str = ""
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Uploads / storage
    UPLOAD_DIR: str = "data/uploads"
    MAX_UPLOAD_MB: int = 25

    # ------------------------------------------------------------------
    # Derived / convenience accessors
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy (asyncpg) connection URL."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            "postgresql+asyncpg://"
            f"{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        """Redis DSN understood by arq and redis-py."""
        if self.REDIS_URL:
            return self.REDIS_URL
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def embedding_model(self) -> str:
        return self.EMBEDDING_MODEL or _DEFAULT_EMBEDDING_MODELS[self.EMBEDDING_PROVIDER]

    @property
    def llm_model(self) -> str:
        return self.LLM_MODEL or _DEFAULT_LLM_MODELS[self.LLM_PROVIDER]

    @property
    def embedding_dim(self) -> int:
        if self.EMBEDDING_DIM is not None:
            return self.EMBEDDING_DIM
        return _DEFAULT_EMBEDDING_DIMS[self.EMBEDDING_PROVIDER]

    @property
    def cors_origins(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


settings = get_settings()
