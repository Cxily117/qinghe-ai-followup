"""问答相关 DTO。"""
from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """提问入参。session_id 为空则新建会话。"""

    question: str = Field(min_length=1, max_length=2000, description="医护人员的问题")
    session_id: int | None = Field(default=None, description="会话 id，可为空（自动新建）")


class SessionOut(BaseModel):
    id: int
    title: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    meta: dict | None = None
    created_at: str | None = None


class AskResponse(BaseModel):
    """非流式回答（/chat/ask）。"""

    session_id: int
    message_id: int
    answer: str
    citations: list[dict]
    flags: dict
    model: str
    latency_ms: int
    prompt_version: str | None = None
