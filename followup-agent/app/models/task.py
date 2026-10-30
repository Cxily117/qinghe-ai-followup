"""随访任务草稿表——LLM 输出的上限。

硬关卡落库的地方：AI 只能写 draft，confirmed 必须由人点击
（对应需求清单「任务止步草稿」的架构决策）。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FollowupTaskDraft(Base):
    __tablename__ = "followup_task_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 来源会话（可为空：人工直接新建的草稿）
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 患者引用（D1–D2 先存外键标识，正式版对接 EMPI 后替换为 patient_id）
    patient_ref: Mapped[str] = mapped_column(String(64), index=True)
    plan_type: Mapped[str] = mapped_column(String(64), default="")  # 病种/随访方案
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 随访项/时间点等结构化内容
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="draft", index=True
    )  # draft|confirmed|cancelled
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
