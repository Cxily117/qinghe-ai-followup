"""智能体相关 DTO。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AgentConfirmIn(BaseModel):
    """人工确认（或取消）一张待落库的随访草稿。

    刻意只允许两个动作：
      - confirm：把确认单里的内容写成 FollowupTaskDraft（状态仍是 draft）；
      - cancel：丢弃，库里不留任何痕迹。
    没有 "edit"：要改内容就重新发起一轮，让模型按新方案重走校验——
    允许在确认接口里改参数，等于绕过了刚刚的合规校验。
    """

    action: Literal["confirm", "cancel"] = Field("confirm", description="confirm=保存草稿；cancel=取消")
    note: str = Field("", max_length=200, description="确认备注，会记入草稿的 confirm 块")
