"""Centralised application configuration.

All settings are sourced from environment variables (or a local ``.env`` file)
and validated by Pydantic v2. Derived values such as the async database URL and
Redis DSN are exposed as computed fields so the rest of the codebase never has
to build connection strings by hand.

Security posture:
    * No secret has an in-code default. A missing database credential or a
      provider API key raises at import time (fail fast) rather than silently
      degrading at runtime.
    * ``DEBUG`` defaults to ``False`` so accidental production deploys do not
      echo SQL or expose verbose error details.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "ollama"]
Environment = Literal["development", "staging", "production"]

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
    ENVIRONMENT: Environment = "development"
    # ``None`` derives the value from the environment: docs are enabled outside
    # production and disabled in production unless explicitly overridden.
    ENABLE_DOCS: bool | None = None
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    CORS_ORIGINS: str = "*"
    METRICS_ENABLED: bool = True

    # Security
    # When set, every /api/v1/* route requires this value in ``API_KEY_HEADER``.
    # Mandatory in production so the API is never deployed wide open.
    API_KEY: str | None = None
    API_KEY_HEADER: str = "X-API-Key"
    # Requests allowed per window, per client, for the expensive routes.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    UPLOAD_RATE_LIMIT_REQUESTS: int = 10
    CHAT_RATE_LIMIT_REQUESTS: int = 30

    # PostgreSQL
    POSTGRES_USER: str = "postgres"
    # Intentionally optional: a value is only required when ``DATABASE_URL`` is
    # not supplied. There is no fallback password by design.
    POSTGRES_PASSWORD: str | None = None
    POSTGRES_DB: str = "docmind"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    # Explicit override; when unset it is derived from the POSTGRES_* values.
    DATABASE_URL: str | None = None

    # Redis / Arq
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
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
    OPENAI_API_KEY: str | None = None
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Uploads / storage
    UPLOAD_DIR: str = "data/uploads"
    MAX_UPLOAD_MB: int = 25

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_database_credentials(self) -> Settings:
        """Require either an explicit URL or a password — never a hidden default."""
        if not self.DATABASE_URL and not self.POSTGRES_PASSWORD:
            raise ValueError("Database is not configured: set DATABASE_URL or POSTGRES_PASSWORD.")
        return self

    @model_validator(mode="after")
    def _validate_rate_limits(self) -> Settings:
        """Guard the rate-limit configuration against nonsensical values."""
        if self.RATE_LIMIT_WINDOW_SECONDS <= 0:
            raise ValueError("RATE_LIMIT_WINDOW_SECONDS must be positive.")
        if self.UPLOAD_RATE_LIMIT_REQUESTS <= 0 or self.CHAT_RATE_LIMIT_REQUESTS <= 0:
            raise ValueError("Rate-limit request budgets must be positive.")
        return self

    @model_validator(mode="after")
    def _validate_production_posture(self) -> Settings:
        """Refuse to boot an insecure production configuration."""
        if self.is_production:
            if not self.API_KEY:
                raise ValueError(
                    "API_KEY is required in production: the HTTP API must not be "
                    "exposed without authentication."
                )
            if self.cors_origins == ["*"]:
                raise ValueError(
                    "CORS_ORIGINS must be an explicit allow-list in production (never '*')."
                )
        return self

    @model_validator(mode="after")
    def _validate_provider_credentials(self) -> Settings:
        """Fail fast when the selected provider has no usable credentials."""
        if "openai" in {self.EMBEDDING_PROVIDER, self.LLM_PROVIDER} and not self.OPENAI_API_KEY:
            raise ValueError(
                "OPENAI_API_KEY is required when EMBEDDING_PROVIDER or "
                "LLM_PROVIDER is set to 'openai'."
            )
        return self

    @model_validator(mode="after")
    def _validate_chunking(self) -> Settings:
        """Guard the chunking invariants relied upon by the indexer."""
        if self.CHUNK_SIZE <= 0:
            raise ValueError("CHUNK_SIZE must be positive.")
        if self.CHUNK_OVERLAP < 0:
            raise ValueError("CHUNK_OVERLAP must be non-negative.")
        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE.")
        return self

    @model_validator(mode="after")
    def _validate_embedding_dim(self) -> Settings:
        """Ensure the configured vector width is physically meaningful."""
        if self.EMBEDDING_DIM is not None and self.EMBEDDING_DIM <= 0:
            raise ValueError("EMBEDDING_DIM must be a positive integer.")
        return self

    # ------------------------------------------------------------------
    # Derived / convenience accessors
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        """Whether the service is running under the production environment."""
        return self.ENVIRONMENT == "production"

    @property
    def docs_enabled(self) -> bool:
        """Whether OpenAPI/Swagger surfaces are exposed."""
        if self.ENABLE_DOCS is not None:
            return self.ENABLE_DOCS
        return not self.is_production

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
        auth = f":{quote(self.REDIS_PASSWORD, safe='')}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def embedding_model(self) -> str:
        """Resolved embedding model name for the active provider."""
        return self.EMBEDDING_MODEL or _DEFAULT_EMBEDDING_MODELS[self.EMBEDDING_PROVIDER]

    @property
    def llm_model(self) -> str:
        """Resolved chat model name for the active provider."""
        return self.LLM_MODEL or _DEFAULT_LLM_MODELS[self.LLM_PROVIDER]

    @property
    def embedding_dim(self) -> int:
        """Vector width, defaulting per provider."""
        if self.EMBEDDING_DIM is not None:
            return self.EMBEDDING_DIM
        return _DEFAULT_EMBEDDING_DIMS[self.EMBEDDING_PROVIDER]

    @property
    def cors_origins(self) -> list[str]:
        """Parsed list of allowed CORS origins."""
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        """Maximum accepted upload size in bytes."""
        return self.MAX_UPLOAD_MB * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached, validated :class:`Settings` instance."""
    return Settings()


settings = get_settings()
