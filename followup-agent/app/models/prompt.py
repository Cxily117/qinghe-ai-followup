"""提示词模板表：把「约束模型行为的资产」从代码文件搬进数据库。

为什么要进库：
  需求是「管理员在线改提示词、改完立即对问答生效」。提示词如果还躺在
  app/agents/prompts/*.md 里，改一次要发版重启，等于没有这个功能。
  存库后管理页可直接写，qa_service 每次问答读生效版本。

为什么不是原地 UPDATE 覆盖：
  提示词是追责材料。质控问「三个月前那句回答是被什么约束的」，
  必须能拿出当时那一版正文。所以每次保存 = 新增一个版本，旧版本永不删除，
  只把 is_active 移走。回滚也不是「把旧记录激活」，而是拿旧正文再存一版——
  否则版本号会倒退，历史顺序就乱了。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        # 同一模板下版本号唯一：并发保存时靠这条约束兜底，不会出现两个 v3
        UniqueConstraint("name", "version", name="uq_prompt_name_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 模板标识：policy_qa（制度问答）/ task_extract（任务抽取）
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    # 本次修改说明：写清楚"为什么改"，否则三个月后没人知道 v4 动了什么
    note: Mapped[str] = mapped_column(String(200), default="")
    # 正文指纹：回答的 meta 里记这个，能把"某条回答"精确对到"某段正文"，
    # 连同一版本号被绕过（直接改库）都能查出来
    content_sha: Mapped[str] = mapped_column(String(16), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
