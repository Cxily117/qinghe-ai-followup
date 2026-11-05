"""患者目录表——随访系统的"人"从哪里来。

为什么要有这张表（而不是继续读 patients.json）：
  1. 以前患者写死在 `app/agents/data/patients.json`，改一个人要改代码、要发版；
     换成表以后，管理员在后台加一条，智能体下一次提问就能查到——这就是"数据驱动"；
  2. 随访草稿的 `patient_ref` 需要有个地方能对得上号。JSON 文件没有唯一约束，
     手抖写两遍同一个住院号，`resolve_patient` 就会返回"匹配到多位同名患者"；
     这里 `patient_ref` 加唯一索引，脏数据在写入口就被挡掉；
  3. 真实部署时这张表就是 HIS/EMPI 的落地表（或视图），
     字段对齐后把 `source` 从 seed 换成 his 即可，上层工具、提示词、护栏都不用改。

数据来源标记 `source`：
  seed  = 仓库自带的演示数据（非真实患者）
  manual= 管理员在后台手工录入
  his   = 从院内系统同步（预留，接入时写入）
"""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 院内患者标识（住院号/门诊号），是本表对外的唯一键
    patient_ref: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    # 随访方案/病种，决定规则引擎用哪套节点要求
    plan_type: Mapped[str] = mapped_column(String(64), default="")
    discharge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 联系方式：只在服务端用于拨号，回答与页面一律掩码显示
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
