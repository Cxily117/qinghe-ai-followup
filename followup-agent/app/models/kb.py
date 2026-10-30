"""知识库表：文档（draft/published 两态）+ 切片（向量）。

合规要点（对应需求清单）：
  - 文档换版走新 version，不覆盖旧版 → 留痕；
  - RAG 只检索 status='published'；
  - 向量单独成表 → 换 embedding 模型只需重建 kb_chunks。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, embedding_column_type


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # 随访方案 | 病种规范 | 知情告知 | 质控要求
    doc_type: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    file_path: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(
        String(16), default="draft", index=True
    )  # draft|published|archived
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KbChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    # pgvector 环境为 vector(1024)，SQLite 开发态为 JSON
    embedding: Mapped[list | None] = mapped_column(embedding_column_type(), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
