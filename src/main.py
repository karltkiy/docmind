from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from src.api.v1.router import api_router
from src.config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic (e.g., database connection pool initialization)
    yield
    # Shutdown logic (e.g., closing connection pools)
    pass

app = FastAPI(
    title="DocMind API",
    description="Enterprise RAG microservice",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router)

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
