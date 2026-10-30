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
    """单次工具调用。seq 即「第几轮决策」，保证顺序可回放。"""

    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # 这一轮模型做了什么：tool_call（调工具）/ blocked（调了未下发的工具）
    # / final（给出答复、本轮无工具）/ confirm（写操作被闸门拦下、等待人工确认）
    decision: Mapped[str] = mapped_column(String(16), default="tool_call")
    tool: Mapped[str] = mapped_column(String(48), index=True)
    args_redacted: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 模型在本轮伴随工具调用说出的话（截断+脱敏）。复盘时能看出"它当时以为自己在干什么"，
    # 单独存一列而不是塞进 args：args 是给工具的参数，两者语义不同。
    model_note: Mapped[str] = mapped_column(String(300), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str] = mapped_column(String(48), default="")
    # 结果摘要（前 250 字，已脱敏），全文不回灌数据库——工具结果可能是整段制度条文
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


class AgentConfirmation(Base):
    """「创建草稿」的人工确认单（阶段 4 · 写操作闸门）。

    为什么要有这张表，而不是让模型"问一句再建"：
      模型问完「可以吗」，下一轮完全可能自己认为"医护已经同意了"就直接建。
      **确认必须是一个服务端可见的状态迁移**，不能是对话里的一句自然语言。
      所以创建工具的执行结果是"挂起"：草稿内容先存到这里，run 状态置
      pending_confirm，只有带着登录态的人调 /confirm 才能把它变成草稿行。

    一次性 + 过期：status 只允许 pending → confirmed / cancelled，
    过期后 confirmed 也走不通，必须重新生成（避免隔夜确认一张按旧制度算的方案）。
    """

    __tablename__ = "agent_confirmations"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    # confirmed|cancelled|expired；pending 是常态
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 待落库的完整草稿参数（已归一化：日期算好、渠道白名单、节点排序去重）
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    patient_ref: Mapped[str] = mapped_column(String(64), default="")
    plan_type: Mapped[str] = mapped_column(String(64), default="")
    rule_sha: Mapped[str] = mapped_column(String(32), default="")
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    # 落库后的草稿 id（仅 confirmed 有值）
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
