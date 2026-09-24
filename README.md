# DocMind API

DocMind API is a production-ready, enterprise-grade RAG (Retrieval-Augmented Generation)
microservice built with Python 3.12, FastAPI, and PostgreSQL with `pgvector`.

## 🚀 Features

- **Asynchronous Architecture**: FastAPI + `asyncpg` + SQLAlchemy 2.0 `AsyncSession`.
- **Background Processing**: `Arq` + `Redis` worker for parsing, chunking and embedding.
- **Pluggable RAG Engine**: switch between local (Ollama) and cloud (OpenAI) providers via config.
- **Vector Search**: pgvector cosine similarity with an HNSW index.
- **Streaming Responses**: server-sent, token-by-token chat via `StreamingResponse`.
- **Schema Migrations**: Alembic (async) with the `vector` extension and HNSW index.
- **Interactive Demo**: Streamlit UI included.

## 🛠 Tech Stack

| Concern | Technology |
| --- | --- |
| Language | Python 3.12+ |
| Framework | FastAPI (async), Pydantic v2 |
| Database | PostgreSQL 16 + pgvector |
| Task queue | Arq + Redis |
| AI models | OpenAI (`gpt-4o-mini`, `text-embedding-3-small`) or Ollama (`llama3.1:8b`, `nomic-embed-text`) |
| Infra | Docker & Docker Compose |

## 📦 Getting Started

### Prerequisites
- Docker and Docker Compose (v2.24+ for optional `env_file`).

### Run everything

```bash
cp .env.example .env          # optional; sensible defaults are built in
docker compose up --build
```

Migrations run automatically before the API starts.

- Swagger UI: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`
- Demo UI: `http://localhost:8501`

### Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d db redis
alembic upgrade head
uvicorn src.main:app --reload
# in another shell
arq src.worker.tasks.WorkerSettings
```

## 🔌 API Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/documents/upload` | Upload a document (`txt`, `md`, `csv`, `json`, `pdf`) → `202` |
| `GET` | `/api/v1/documents` | List documents |
| `GET` | `/api/v1/documents/{id}/status` | Processing status + chunk count |
| `DELETE` | `/api/v1/documents/{id}` | Delete a document, its chunks and stored file |
| `POST` | `/api/v1/chat/completions` | SSE-streamed, grounded answer |
| `GET` | `/health` | Liveness + database/Redis health |

### SSE event format

Chat returns `text/event-stream` frames:

```
data: {"type":"sources","content":[{"document_id":"...","chunk_index":0,"score":0.81,"excerpt":"..."}]}

data: {"type":"token","content":"Hello"}

data: {"type":"done"}
```

Errors are delivered as `{"type":"error","message":"..."}`.

## ⚙️ Configuration

All settings live in [`src/config.py`](src/config.py:1) and are overridable via environment
variables or `.env`. Key options:

| Variable | Default | Notes |
| --- | --- | --- |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `localhost` / `5432` | Compose overrides host to `db` |
| `DATABASE_URL` | derived | Explicit async DSN override |
| `REDIS_HOST` / `REDIS_PORT` | `localhost` / `6379` | Compose overrides host to `redis` |
| `EMBEDDING_PROVIDER` / `LLM_PROVIDER` | `openai` | `openai` or `ollama` |
| `EMBEDDING_MODEL` / `LLM_MODEL` | per provider | Override model names |
| `EMBEDDING_DIM` | per provider (1536/768) | Must match the model & migration |
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Required for Ollama |
| `UPLOAD_DIR` | `data/uploads` | Shared volume in Compose |
| `MAX_UPLOAD_MB` | `25` | Upload size limit |

> **Embedding dimension**: if you switch to a model with a different vector size,
> update `EMBEDDING_DIM` **and** regenerate the Alembic migration, then re-index.

## 🏗 Project Structure

```text
docmind/
├── alembic/
│   ├── env.py                  # Async Alembic environment
│   └── versions/0001_initial.py
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── demo/app_ui.py              # Streamlit UI
├── src/
│   ├── main.py                 # App factory, lifespan, /health
│   ├── config.py               # Pydantic settings
│   ├── api/v1/
│   │   ├── router.py
│   │   ├── documents.py
│   │   └── chat.py
│   ├── db/
│   │   ├── base.py             # Engine/session factory
│   │   └── models.py           # Document, DocumentChunk
│   ├── schemas/                # Pydantic v2 schemas
│   ├── services/
│   │   ├── rag_engine.py       # Provider-agnostic LLM/embeddings
│   │   ├── vector_store.py     # pgvector search
│   │   ├── chunker.py          # Token-aware chunking
│   │   └── parser.py           # PDF/TXT extraction
│   └── worker/
│       ├── tasks.py            # Arq tasks + WorkerSettings
│       └── queue.py            # Shared Redis pool
└── tests/
```

## 🧪 Tests

```bash
pip install -e ".[dev]"
pytest
ruff check .
mypy src
```

## 🛠 Troubleshooting

- **`/health` returns `degraded`**: Postgres or Redis is unreachable — check `docker compose ps`.
- **Uploads stay `processing`**: ensure the `worker` container is running and can reach Redis.
- **`415 Unsupported Media Type`**: only `txt`, `md`, `csv`, `json`, `pdf` are accepted.
- **Dimension mismatch from pgvector**: set `EMBEDDING_DIM` to match your model and re-run migrations.
