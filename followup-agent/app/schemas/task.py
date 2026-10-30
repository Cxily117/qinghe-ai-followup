"""随访任务的请求/响应模型（D5）。

有一条刻意的设计：`TaskDraftOut` 里有 `status`，但**任何入参模型里都没有 status**。
草稿的落地状态只能由服务端写（"draft"）或由人调用确认接口改（"confirmed"），
客户端无法提交一个「已确认」来绕过人工关卡。
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class TaskItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    offset_days: int = Field(ge=0, le=365, description="相对出院日的天数")
    channel: str = Field(default="phone", description="phone|sms|wecom|outpatient")
    notes: str = Field(default="", max_length=200)


class TaskItemOut(TaskItemIn):
    # due_date 由服务端算好回填，前端不必再自己算日期
    due_date: date | None = None


class TaskDraftGenerateIn(BaseModel):
    """两个入口二选一：给人话（question）或给会话（session_id）。"""

    session_id: int | None = Field(default=None, description="从该会话的最近对话里抽取")
    question: str | None = Field(default=None, max_length=2000, description="直接给一段随访安排描述")
    patient_ref: str | None = Field(default=None, max_length=64, description="已知患者标识，模型没抽到时的兜底")
    base_date: date | None = Field(default=None, description="出院日（基准日），默认今天")


class TaskConfirmIn(BaseModel):
    """人工确认时可以顺手修正——确认的是「改过的这份」，不是 AI 那份。"""

    plan_type: str | None = Field(default=None, max_length=64)
    scheduled_at: datetime | None = None
    items: list[TaskItemIn] | None = None
    note: str | None = Field(default=None, max_length=200)


class TaskDraftOut(BaseModel):
    id: int
    session_id: int | None
    patient_ref: str
    plan_type: str
    scheduled_at: datetime | None
    status: str
    content: dict | None
    confirmed_by: int | None = None
    created_by: int
    created_at: datetime | None = None
