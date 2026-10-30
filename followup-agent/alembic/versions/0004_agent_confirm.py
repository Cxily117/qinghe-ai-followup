"""确认闸门 + 逐轮决策日志字段

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30

说明（阶段 4 · P2）：
  1. 新增 agent_confirmations：创建草稿被闸门拦下时，把「待落库的草稿内容」
     存成一张待确认单，run 状态置 pending_confirm；只有人调 /confirm 才会
     真正写 followup_task_drafts。这张表是「确认必须有状态」的物理载体。
  2. agent_tool_calls 增加 decision / model_note：原表只记了"调了什么工具"，
     复盘时缺"这一轮模型想干什么"（是调工具、给答复，还是试图越权）。
     decision 让每个 seq 变成一次可读的决策记录，而不只是一行调用日志。
  3. 全部为新增，不改旧列语义；已有数据零风险。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 1. 逐轮决策字段 ----
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.add_column(
            sa.Column("decision", sa.String(16), nullable=False, server_default="tool_call")
        )
        batch.add_column(
            sa.Column("model_note", sa.String(300), nullable=False, server_default="")
        )

    # ---- 2. 确认单 ----
    op.create_table(
        "agent_confirmations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("patient_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("plan_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("rule_sha", sa.String(32), nullable=False, server_default=""),
        sa.Column("items_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draft_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_confirmations_run_id", "agent_confirmations", ["run_id"])
    op.create_index("ix_agent_confirmations_status", "agent_confirmations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_confirmations_status", table_name="agent_confirmations")
    op.drop_index("ix_agent_confirmations_run_id", table_name="agent_confirmations")
    op.drop_table("agent_confirmations")
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.drop_column("model_note")
        batch.drop_column("decision")
