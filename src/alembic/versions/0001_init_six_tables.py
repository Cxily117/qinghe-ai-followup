"""初始化六张表

Revision ID: 0001
Revises:
Create Date: 2026-10-30

双方言设计：
  - PostgreSQL：kb_chunks.embedding 使用 pgvector vector(1024) + HNSW 索引
  - SQLite（开发态）：降级为 JSON 列，仅存储不做 ANN 检索
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _embedding_type():
    """与 app/models/base.py 的 embedding_column_type 保持一致。"""
    if _is_postgres():
        from pgvector.sqlalchemy import Vector

        return Vector(1024)
    return sa.JSON()


def upgrade() -> None:
    is_pg = _is_postgres()

    # PostgreSQL 需先启用向量扩展（HNSW 索引依赖 pgvector>=0.5）
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---------- 1. users ----------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("full_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False, server_default="staff"),
        sa.Column("department", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # ---------- 2. sessions ----------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(128), nullable=False, server_default="新会话"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # ---------- 3. messages ----------
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(),
                  sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])

    # ---------- 4. kb_documents ----------
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("doc_type", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("file_path", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_kb_documents_doc_type", "kb_documents", ["doc_type"])
    op.create_index("ix_kb_documents_status", "kb_documents", ["status"])

    # ---------- 5. kb_chunks（向量表） ----------
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(),
                  sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", _embedding_type(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"])
    if is_pg:
        # 余弦距离 HNSW 索引；知识库规模小时也可暂不建
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding_hnsw "
            "ON kb_chunks USING hnsw (embedding vector_cosine_ops)"
        )

    # ---------- 6. followup_task_drafts ----------
    op.create_table(
        "followup_task_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(),
                  sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("patient_ref", sa.String(64), nullable=False),
        sa.Column("plan_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_followup_task_drafts_session_id",
                    "followup_task_drafts", ["session_id"])
    op.create_index("ix_followup_task_drafts_patient_ref",
                    "followup_task_drafts", ["patient_ref"])
    op.create_index("ix_followup_task_drafts_status",
                    "followup_task_drafts", ["status"])


def downgrade() -> None:
    op.drop_table("followup_task_drafts")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("users")
