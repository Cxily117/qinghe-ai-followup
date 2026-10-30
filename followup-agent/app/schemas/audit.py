"""审计相关 DTO。"""
from typing import Literal

from pydantic import BaseModel, Field


class MarkIn(BaseModel):
    # high_risk = 疑似高危（病情异常预警类）；watch = 先观察
    level: Literal["high_risk", "watch"] = "high_risk"
    note: str = Field(default="", max_length=500, description="定性依据，写清为什么判为高危")
    message_id: int | None = Field(default=None, description="钉到具体某条回答，可空")
