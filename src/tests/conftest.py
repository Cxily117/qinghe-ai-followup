"""测试公共夹具：一个真库（SQLite 文件）、一个会话、两个角色的人。

为什么用真库而不是 mock：
  service 层的一半逻辑在 SQL 里（去重窗口、版本号 +1、软撤回过滤、分页），
  把 db 换成 Mock 就等于把这部分逻辑测掉了、却测不出问题。用 SQLite 临时文件
  + `Base.metadata.create_all` 跑真语句，代价是几十毫秒，换来的是「迁移能建表」
  之外的第二道结构校验：模型字段一改名，这些用例立刻红。

为什么每个用例一个库文件：用例之间不串数据，失败时也不互相污染；
比 :memory: 用 StaticPool 更简单，且能覆盖「连接复用」这类真实场景。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, ChatSession, Message, User


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[AsyncSession]:
    """每个用例独立的空库 + 一个会话。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session

    await engine.dispose()


async def _make_user(db: AsyncSession, username: str, role: str, **extra) -> User:
    user = User(
        username=username,
        password_hash="pbkdf2$test$not-a-real-hash",
        full_name=extra.pop("full_name", username),
        role=role,
        department=extra.pop("department", None),
        **extra,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def staff(db: AsyncSession) -> User:
    return await _make_user(db, "nurse01", "staff", department="骨科")


@pytest_asyncio.fixture
async def other_staff(db: AsyncSession) -> User:
    return await _make_user(db, "nurse02", "staff", department="心内科")


@pytest_asyncio.fixture
async def admin(db: AsyncSession) -> User:
    return await _make_user(db, "admin01", "admin", department="护理部")


async def add_session(
    db: AsyncSession,
    *,
    user_id: int,
    title: str = "术后随访咨询",
    messages: list[dict] | None = None,
    updated_minutes_ago: int = 0,
) -> ChatSession:
    """造一条会话（可带消息）。messages 里每项支持 role/content/meta。"""
    now = datetime.now() - timedelta(minutes=updated_minutes_ago)
    session = ChatSession(user_id=user_id, title=title, created_at=now, updated_at=now)
    db.add(session)
    await db.flush()
    for m in messages or []:
        db.add(
            Message(
                session_id=session.id,
                role=m.get("role", "user"),
                content=m.get("content", ""),
                meta=m.get("meta"),
            )
        )
    await db.commit()
    await db.refresh(session)
    return session
