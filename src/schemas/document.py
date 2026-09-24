from typing import List, Optional
from uuid import UUID
from pydantic import BaseModel, Field

class DocumentBase(BaseModel):
    filename: str
    status: str = "processing"

class DocumentCreate(DocumentBase):
    pass

class DocumentResponse(DocumentBase):
    id: UUID
    chunk_count: int

    model_config = {
        "from_attributes": True
    }
