from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Database
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "docmind"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # RAG Configuration
    EMBEDDING_PROVIDER: str = "openai"  # "ollama" or "openai"
    LLM_PROVIDER: str = "openai"        # "ollama" or "openai"

    # OpenAI Credentials
    OPENAI_API_KEY: str = ""

    # Ollama Configuration
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Application Settings
    DEBUG: bool = True
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    model_config = {
        "env_file": ".env",
        "extra": "ignore"
    }

settings = Settings()
