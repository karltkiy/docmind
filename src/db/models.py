import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Column, String, Integer, Text, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

from .base import Base

class Document(Base):
    __tablename__ = "documents"

    id = Column(uuid.UUID, primary_key=True, default=uuid.uuid4)
    filename = Column(String(255), nullable=False)
    status = Column(String(50), default="processing")  # processing, completed, failed
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(uuid.UUID, primary_key=True, default=uuid.uuid4)
    document_id = Column(uuid.UUID, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    metadata_ = Column(JSONB, nullable=True)
    embedding = Column(Vector(1536))

    document = relationship("Document", back_populates="chunks")

    __table_args__ = (
        # HNSW index for fast vector lookup
        # Note: pgvector.sqlalchemy.Vector(1536) is used for the column type
        # The index is created via SQL or Alembic, but we can define it here if needed.
        # However, standard practice is to let Alembic handle the specific index parameters.
    )
