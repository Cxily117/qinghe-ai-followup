"""知识库相关 DTO。"""
from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: int
    title: str
    doc_type: str
    version: int
    status: str
    chunks: int | None = None


class ChunkOut(BaseModel):
    chunk_id: int
    chunk_index: int
    content: str
    token_count: int


class KbStats(BaseModel):
    documents_total: int
    published_chunks: int
    embed_provider: str
    embed_dim: int
