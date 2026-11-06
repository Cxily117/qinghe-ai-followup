"""智能体运行审计三表

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30

说明：
  - 只建三张新表，不动前 8 张（已有数据零风险）；
  - agent_tool_calls 用 ondelete=CASCADE 挂在 agent_runs 上：删会话时日志一起清，
    但会话本身不做物理删除，所以实际上不会丢审计；
  - ticket_nonces.nonce 唯一索引是防重放的唯一防线（并发下靠数据库，不靠服务层判断）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_uid", sa.String(32), nullable=False),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="bounded"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("question", sa.String(2000), nullable=False, server_default=""),
        sa.Column("route", sa.JSON(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("answer_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(48), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_run_uid", "agent_runs", ["run_uid"], unique=True)
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])

    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool", sa.String(48), nullable=False),
        sa.Column("args_redacted", sa.JSON(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_code", sa.String(48), nullable=False, server_default=""),
        sa.Column("result_digest", sa.String(256), nullable=False, server_default=""),
        sa.Column("ticket_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_tool_calls_run_id", "agent_tool_calls", ["run_id"])
    op.create_index("ix_agent_tool_calls_tool", "agent_tool_calls", ["tool"])

    op.create_table(
        "ticket_nonces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False, server_default=""),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ticket_nonces_nonce", "ticket_nonces", ["nonce"], unique=True)


def downgrade() -> None:
    op.drop_table("ticket_nonces")
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_runs")
