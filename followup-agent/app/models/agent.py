"""智能体运行审计（阶段 4）。

为什么每一次工具调用都要落库：
  自主决策的代价是「过程不可预测」。出了问题如果只能看最终那句话，
  就永远说不清「它当时为什么跳过了校验」。
  这三张表是回放能力的底座——质检可以逐轮看到：第几步、调了什么工具、
  带了哪张票据、返回了什么摘要、耗时多少。

隐私边界：`args_redacted` 存的是**脱敏后**的入参，患者姓名与住院号只留掩码；
原始入参不入库，避免审计表变成第二个 PHI 泄露面。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentRun(Base):
    """一次问答请求对应的完整智能体运行。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 对外暴露的运行标识（不连续、不可猜），前端与审计都用它引用
    run_uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="bounded")
    # running|succeeded|refused|handoff|max_steps|failed
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    question: Mapped[str] = mapped_column(String(2000), default="")
    # 用了哪些工具、跳过了哪些步骤——「路线摘要」比全文更适合做统计
    route: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    answer_chars: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(48), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentToolCall(Base):
    """单次工具调用。seq 保证顺序可回放。"""

    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    tool: Mapped[str] = mapped_column(String(48), index=True)
    args_redacted: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str] = mapped_column(String(48), default="")
    # 结果摘要（前 200 字），全文不回灌数据库——工具结果可能是整段制度条文
    result_digest: Mapped[str] = mapped_column(String(256), default="")
    ticket_id: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TicketNonce(Base):
    """票据一次性凭据。

    签名只能证明「这张票是我签的」，证明不了「它没被用过」。
    重放攻击（拿同一个 rule_ticket 连建两张草稿）必须靠这张表挡住。
    """

    __tablename__ = "ticket_nonces"

    id: Mapped[int] = mapped_column(primary_key=True)
    nonce: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
