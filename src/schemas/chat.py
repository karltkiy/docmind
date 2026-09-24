from typing import List, Optional
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    query: str
    top_k: int = Field(default=4, ge=1, le=20)
    document_ids: Optional[List[str]] = None
