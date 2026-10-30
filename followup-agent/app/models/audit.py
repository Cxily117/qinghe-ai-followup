"""对话审计标记表：管理员对「疑似高危随访问答」的定性结论。

设计取舍：
  1. 标记挂在**会话**上（message_id 可空），因为审计的动作单位是"这次问诊"，
     但允许钉到某一条回答——高危往往只在某一轮出现，钉住才好复核；
  2. 取消标记用软撤回（revoked_at），不物理删。审计痕迹不能由被审对象之外的人
     悄悄抹掉，"谁在什么时候撤销了什么"本身就是要留档的事实；
  3. 自动预筛（flags）与人工标记分开存：前者是 messages.meta 里的机器判断，
     后者是本表里的人签字。两者不能混，否则报表里分不清"AI 觉得危险"和"人确认危险"。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChatAuditMark(Base):
    __tablename__ = "chat_audit_marks"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    # 钉在具体哪条回答上（可选）；为空表示"整个会话"
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    # high_risk=疑似高危（病情异常预警类）| watch=观察
    level: Mapped[str] = mapped_column(String(16), default="high_risk", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    marked_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    revoked_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
