"""会话服务：会话生命周期与消息落库（含溯源 meta）。

设计要点：
  - 每次问答都必须留下 user + assistant 两条消息，assistant 的 meta 里记
    citations（引用了哪些切片）、model、latency_ms、prompt_version、flags；
    这是「AI 当时依据什么说的」唯一可举证的地方；
  - 会话归属强校验：任何按 id 访问的地方都带 user_id 条件，防越权读别人的问诊记录。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatSession, Message


async def create_session(db: AsyncSession, user_id: int, title: str = "新会话") -> ChatSession:
    session = ChatSession(user_id=user_id, title=title[:60] or "新会话")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session_for_user(
    db: AsyncSession, session_id: int, user_id: int
) -> ChatSession | None:
    return (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def get_or_create_session(
    db: AsyncSession, user_id: int, session_id: int | None, question: str
) -> ChatSession:
    """带 session_id 就用（校验归属），不带就新建；新建时用首问前 20 字当标题。"""
    if session_id is not None:
        session = await get_session_for_user(db, session_id, user_id)
        if session is None:
            raise LookupError("会话不存在或无权访问")
        return session
    return await create_session(db, user_id, title=question[:20])


async def list_sessions(db: AsyncSession, user_id: int, limit: int = 50) -> list[ChatSession]:
    return (
        await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
            .limit(limit)
        )
    ).scalars().all()


async def list_messages(
    db: AsyncSession, session_id: int, user_id: int, limit: int = 100
) -> list[Message] | None:
    """返回 None 表示会话不存在/无权访问（由路由转 404）。"""
    if await get_session_for_user(db, session_id, user_id) is None:
        return None
    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def recent_history(db: AsyncSession, session_id: int, rounds: int = 6) -> list[dict]:
    """取最近 N **轮**对话作为多轮上下文（升序返回，仅 role/content）。

    「轮」的口径：1 轮 = 1 条 user + 1 条 assistant，所以 6 轮要取 12 条消息。
    早先版本按「条」取 6 条，实际只有 3 轮——上下文比预期短一半，
    模型接不住「刚才那个患者」这种指代。
    """
    limit = max(1, rounds) * 2
    rows = (
        await db.execute(
            select(Message.role, Message.content)
            .where(Message.session_id == session_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
    ).all()
    history = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    # 截断点若落在 assistant 上，说明这半轮没有对应的提问，去掉——
    # 否则模型会看到一段"没有问题的回答"，容易把它当成新的用户意图
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


async def append_message(
    db: AsyncSession,
    session_id: int,
    role: str,
    content: str,
    meta: dict | None = None,
) -> Message:
    message = Message(session_id=session_id, role=role, content=content, meta=meta)
    db.add(message)
    # 顺带刷新会话的 updated_at，让会话列表按最近活跃排序
    session = await db.get(ChatSession, session_id)
    if session is not None:
        session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(message)
    return message
