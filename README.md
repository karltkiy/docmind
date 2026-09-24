# DocMind API

DocMind API is a production-ready, enterprise-grade RAG (Retrieval-Augmented Generation) microservice built with Python 3.12, FastAPI, and PostgreSQL with `pgvector`.

## 🚀 Features

- **Asynchronous Architecture**: Built with FastAPI and `asyncpg` for high-concurrency performance.
- **Robust Data Processing**: Background task processing using `Arq` and `Redis` for document parsing and embedding generation.
- **Pluggable RAG Engine**: Seamlessly switch between local (Ollama) and cloud (OpenAI) providers via environment configuration.
- **Vector Search**: High-performance similarity search using `pgvector` with HNSW indexing.
- **Streaming Responses**: Real-time chat experience using Server-Sent Events (SSE).
- **Interactive Demo**: A built-in Streamlit UI for easy testing and demonstration.

## 🛠 Tech Stack

- **Language**: Python 3.12+
- **Framework**: FastAPI
- **Database**: PostgreSQL 16 + `pgvector`
- **Task Queue**: Arq + Redis
- **AI Models**: OpenAI (GPT-4o-mini, text-embedding-3-small) or Ollama (Llama 3.1, nomic-embed-text)
- **Infrastructure**: Docker & Docker Compose

## 🚀 Getting Started

### Prerequisites
- Docker and Docker Compose installed.

### Installation & Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/your-repo/docmind-api.git
   cd docmind-api
   ```

2. **Configure Environment**:
   Copy the example environment file and update it with your credentials:
   ```bash
   cp .env.example .env
   ```

3. **Spin up the infrastructure**:
   Start all services (Postgres, Redis, Worker, API, and Demo UI) using Docker Compose:
   ```bash
   docker compose up --build
   ```

## 📖 API Documentation

Once the service is running, you can access the interactive API documentation:
- **Swagger UI**: `http://localhost:8000/docs`
- **Redoc**: `http://localhost:8000/redoc`

## 🚀 Demo UI

The Streamlit demo is available at:
- **Demo UI**: `http://localhost:8501`

## 🏗 Project Structure

```text
docmind-api/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── README.md
├── alembic/
├── src/
│   ├── main.py                # FastAPI app initialization
│   ├── config.py              # Pydantic settings
│   ├── db/
│   │   ├── base.py            # AsyncEngine & Session setup
│   │   ├── models.py          # SQLAlchemy models
│   ├── schemas/               # Pydantic v2 schemas
│   ├── services/
│   │   ├── rag_engine.py      # LLM & Embedding logic
│   │   ├── vector_store.py    # pgvector queries
│   │   ├── chunker.py         # Text processing
│   ├── worker/
│   │   ├── tasks.py           # Arq background tasks
│   ├── api/
│   │   ├── v1/
│   │   │   ├── router.py      # Main API Router
│   │   │   ├── documents.py   # Upload & Status
│   │   │   ├── chat.py        # SSE Chat Streaming
├── demo/
│   ├── app_ui.py             # Streamlit UI
```

## 🛠 Development

- **Linting & Formatting**: Use `ruff` for fast linting and formatting.
- **Type Checking**: Use `mypy` to ensure strict type safety.
- **Migrations**: Use `alembic` to manage database schema changes.
