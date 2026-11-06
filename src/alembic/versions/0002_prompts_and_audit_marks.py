"""提示词模板 + 对话审计标记

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30

说明：
  - 只建两张新表，不动 0001 的六张表（已有数据零风险）；
  - prompt_templates 上加部分唯一索引，保证同一模板「同一时刻只能有一个版本生效」。
    这条约束交给数据库而不是服务层，是因为并发保存时服务层的 check-then-write
    一定会有竞态窗口，而"两个版本同时生效"会让问答用哪一版变得不可复现。
  - 种子数据不写在迁移里（迁移只做 DDL）。种子在应用启动时由
    prompt_service.ensure_seed 完成，正文可读、可测、失败不影响建表。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    # ---------- 1. prompt_templates ----------
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("content_sha", sa.String(16), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_prompt_name_version"),
    )
    op.create_index("ix_prompt_templates_name", "prompt_templates", ["name"])
    op.create_index("ix_prompt_templates_is_active", "prompt_templates", ["is_active"])
    # 每个模板只允许一条 is_active —— 布尔字面量两种方言写法不同
    if _is_postgres():
        op.execute(
            "CREATE UNIQUE INDEX uq_prompt_active_per_name "
            "ON prompt_templates (name) WHERE is_active"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_prompt_active_per_name "
            "ON prompt_templates (name) WHERE is_active = 1"
        )

    # ---------- 2. chat_audit_marks ----------
    op.create_table(
        "chat_audit_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("level", sa.String(16), nullable=False, server_default="high_risk"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("marked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_chat_audit_marks_session_id", "chat_audit_marks", ["session_id"])
    op.create_index("ix_chat_audit_marks_level", "chat_audit_marks", ["level"])
    op.create_index("ix_chat_audit_marks_created_at", "chat_audit_marks", ["created_at"])


def downgrade() -> None:
    op.drop_table("chat_audit_marks")
    op.execute("DROP INDEX IF EXISTS uq_prompt_active_per_name")
    op.drop_table("prompt_templates")
