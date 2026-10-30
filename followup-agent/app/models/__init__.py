"""模型包：集中 import 保证 Base.metadata 完整（Alembic autogenerate 依赖此文件）。"""
from app.models.audit import ChatAuditMark
from app.models.base import Base
from app.models.chat import ChatSession, Message
from app.models.kb import KbChunk, KbDocument
from app.models.prompt import PromptTemplate
from app.models.task import FollowupTaskDraft
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "ChatSession",
    "Message",
    "KbDocument",
    "KbChunk",
    "FollowupTaskDraft",
    "PromptTemplate",
    "ChatAuditMark",
]
