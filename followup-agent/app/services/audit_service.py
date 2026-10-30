"""对话审计服务：按人/按时间检索会话、汇总风险标签、人工标记高危。

两个口径必须分清（这是审计页可信的前提）：
  - **自动预筛**：assistant 消息 meta.flags 里的机器判断（红旗问题、疑似用药建议、
    无引用作答…）。它只说明"这轮回答看起来有风险"，不构成结论。
  - **人工标记**：chat_audit_marks 里带人名的记录。它才是审计结论，
    可以被撤销（软撤回），但撤销同样留痕。

实现上的一个取舍：
  按 flags 过滤无法跨 SQLite/PostgreSQL 统一（SQLite 的 JSON 不能走 GIN 索引），
  所以先在 SQL 里做「人 + 时间」的粗筛，再在 Python 里聚合 flags 并过滤。
  会话量级在千级以内完全够用；生产切 PG 后应改为在 sessions 上冗余 risk_flags 列
  并建 GIN 索引，这一点已记在 README 的已知限制里。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.audit import ChatAuditMark
from app.models.chat import ChatSession, Message
from app.models.user import User

logger = logging.getLogger(__name__)

# 参与「疑似高危」判定的标签（与 guardrails 写入的 key 对应）
RISK_FLAGS = (
    "red_flag_question",
    "medical_advice_suspected",
    "uncited_answer",
    "suspected_general_knowledge",
    "dropped_citations",
)

FLAG_LABELS = {
    "red_flag_question": "红旗问题",
    "medical_advice_suspected": "疑似用药建议",
    "uncited_answer": "无引用作答",
    "suspected_general_knowledge": "疑似通用知识",
    "dropped_citations": "越界引用",
    "refusal": "拒答",
    "empty_kb": "知识库为空",
    "stream_error": "流式中断",
}

LEVEL_LABELS = {"high_risk": "疑似高危", "watch": "观察"}

MAX_SCAN = 500  # 单次扫描的会话上限（分页之前的粗筛），防止一次拉全库


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min)


def _day_end(d: date) -> datetime:
    return datetime.combine(d, time.max)


async def list_staff(db: AsyncSession) -> list[User]:
    """审计筛选用：只列医护（管理员不进审计范围，避免自审）。"""
    return list(
        (
            await db.execute(
                select(User).where(User.role == "staff").order_by(User.id.asc()).limit(200)
            )
        ).scalars().all()
    )


async def _flags_by_session(db: AsyncSession, session_ids: list[int]) -> dict[int, dict]:
    """一次查出这些会话的全部 assistant meta，在内存里聚合。"""
    if not session_ids:
        return {}

    rows = (
        await db.execute(
            select(Message.session_id, Message.meta, Message.created_at)
            .where(Message.session_id.in_(session_ids), Message.role == "assistant")
        )
    ).all()

    agg: dict[int, dict] = {}
    for session_id, meta, _created in rows:
        item = agg.setdefault(session_id, {"flags": set(), "messages": 0, "last_at": None})
        item["messages"] += 1
        for key in (meta or {}).get("flags") or {}:
            item["flags"].add(key)
    return agg


async def _marks_by_session(db: AsyncSession, session_ids: list[int]) -> dict[int, list[ChatAuditMark]]:
    if not session_ids:
        return {}
    rows = (
        await db.execute(
            select(ChatAuditMark)
            .where(
                ChatAuditMark.session_id.in_(session_ids),
                ChatAuditMark.revoked_at.is_(None),
            )
            .order_by(ChatAuditMark.id.desc())
        )
    ).scalars().all()
    out: dict[int, list[ChatAuditMark]] = {}
    for mark in rows:
        out.setdefault(mark.session_id, []).append(mark)
    return out


async def query_sessions(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    only_flagged: bool = False,
    only_marked: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """审计列表：返回 {items, total, page, page_size, summary}。"""
    stmt = (
        select(ChatSession, User)
        .join(User, User.id == ChatSession.user_id)
        .where(User.role == "staff")
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .limit(MAX_SCAN)
    )
    if user_id:
        stmt = stmt.where(ChatSession.user_id == user_id)
    if date_from:
        stmt = stmt.where(ChatSession.updated_at >= _day_start(date_from))
    if date_to:
        stmt = stmt.where(ChatSession.updated_at <= _day_end(date_to))

    rows = (await db.execute(stmt)).all()
    session_ids = [r[0].id for r in rows]

    flags_map = await _flags_by_session(db, session_ids)
    marks_map = await _marks_by_session(db, session_ids)

    items: list[dict] = []
    for session, user in rows:
        agg = flags_map.get(session.id, {"flags": set(), "messages": 0})
        flags = sorted(agg["flags"])
        risk_hits = [f for f in flags if f in RISK_FLAGS]
        marks = marks_map.get(session.id, [])

        items.append(
            {
                "session_id": session.id,
                "title": session.title,
                "user_id": user.id,
                "user_name": user.full_name or user.username,
                "department": user.department or "",
                "messages": agg["messages"],
                "flags": flags,
                "flag_labels": [FLAG_LABELS.get(f, f) for f in flags],
                "risk_count": len(risk_hits),
                # 自动预筛结论：只是候选，不是定性
                "suspected": bool(risk_hits),
                "marked": bool(marks),
                "marks": [
                    {
                        "id": m.id,
                        "level": m.level,
                        "level_label": LEVEL_LABELS.get(m.level, m.level),
                        "note": m.note,
                        "created_at": m.created_at,
                    }
                    for m in marks
                ],
                "updated_at": session.updated_at or session.created_at,
            }
        )

    if only_flagged:
        items = [i for i in items if i["suspected"]]
    if only_marked:
        items = [i for i in items if i["marked"]]

    total = len(items)
    start = max(0, (page - 1) * page_size)
    return {
        "items": items[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "summary": {
            "scanned": len(rows),
            "suspected": sum(1 for i in items if i["suspected"]),
            "marked": sum(1 for i in items if i["marked"]),
        },
    }


async def session_detail(db: AsyncSession, session_id: int) -> dict:
    """会话明细：逐条消息 + 引用文档名 + 标记记录。"""
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise AppError("SESSION_NOT_FOUND")

    user = await db.get(User, session.user_id)
    messages = list(
        (
            await db.execute(
                select(Message).where(Message.session_id == session_id).order_by(Message.id.asc())
            )
        ).scalars().all()
    )
    marks = list(
        (
            await db.execute(
                select(ChatAuditMark)
                .where(ChatAuditMark.session_id == session_id)
                .order_by(ChatAuditMark.id.desc())
            )
        ).scalars().all()
    )

    # 标记人姓名：一次查出，避免模板里逐条再查库
    operator_ids = {m.marked_by for m in marks} | {m.revoked_by for m in marks if m.revoked_by}
    operators = {}
    if operator_ids:
        for u in (
            await db.execute(select(User).where(User.id.in_(operator_ids)))
        ).scalars().all():
            operators[u.id] = u.full_name or u.username

    out_messages = []
    for m in messages:
        meta = m.meta or {}
        flags = sorted((meta.get("flags") or {}).keys())
        out_messages.append(
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
                "flags": flags,
                "flag_labels": [FLAG_LABELS.get(f, f) for f in flags],
                "citations": meta.get("citation_detail") or [],
                "model": meta.get("model"),
                "prompt_version": meta.get("prompt_version"),
                "latency_ms": meta.get("latency_ms"),
                "mark": next((mk for mk in marks if mk.message_id == m.id and not mk.revoked_at), None),
            }
        )

    return {
        "session": {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        },
        "user": {
            "id": user.id if user else 0,
            "name": (user.full_name or user.username) if user else "已删除用户",
            "department": user.department if user else "",
        },
        "messages": out_messages,
        "marks": [
            {
                "id": mk.id,
                "level": mk.level,
                "level_label": LEVEL_LABELS.get(mk.level, mk.level),
                "note": mk.note,
                "created_at": mk.created_at,
                "message_id": mk.message_id,
                "marked_by": operators.get(mk.marked_by, "-"),
                "revoked_at": mk.revoked_at,
                "revoked_by": operators.get(mk.revoked_by, "-") if mk.revoked_by else None,
            }
            for mk in marks
        ],
        "active_marks": [mk for mk in marks if mk.revoked_at is None],
        "suspected": any(
            f in RISK_FLAGS for msg in out_messages for f in msg["flags"]
        ),
    }


async def add_mark(
    db: AsyncSession,
    *,
    session_id: int,
    admin_id: int,
    level: str = "high_risk",
    note: str = "",
    message_id: int | None = None,
) -> ChatAuditMark:
    """人工标记。同一会话同一级别不重复标记（重复点击返回已有记录）。"""
    session = await db.get(ChatSession, session_id)
    if session is None:
        raise AppError("SESSION_NOT_FOUND")
    if level not in LEVEL_LABELS:
        raise AppError("VALIDATION_ERROR", "标记级别只能是 high_risk 或 watch")

    if message_id is not None:
        msg = await db.get(Message, message_id)
        # 钉错会话是最容易出的错：标记挂到 A 会话、消息来自 B 会话，审计就串了
        if msg is None or msg.session_id != session_id:
            raise AppError("VALIDATION_ERROR", "该消息不属于此会话，无法钉选")

    existing = (
        await db.execute(
            select(ChatAuditMark).where(
                ChatAuditMark.session_id == session_id,
                ChatAuditMark.level == level,
                ChatAuditMark.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    mark = ChatAuditMark(
        session_id=session_id,
        message_id=message_id,
        level=level,
        note=(note or "").strip()[:500],
        marked_by=admin_id,
    )
    db.add(mark)
    await db.commit()
    await db.refresh(mark)
    logger.info("会话 %s 被标记为 %s by user=%s", session_id, level, admin_id)
    return mark


async def revoke_mark(db: AsyncSession, *, mark_id: int, admin_id: int) -> ChatAuditMark:
    """软撤回：保留记录与操作人，不物理删除。"""
    mark = await db.get(ChatAuditMark, mark_id)
    if mark is None:
        raise AppError("MARK_NOT_FOUND")
    if mark.revoked_at is not None:
        return mark
    mark.revoked_at = datetime.now()
    mark.revoked_by = admin_id
    await db.commit()
    await db.refresh(mark)
    logger.info("标记 %s 被撤销 by user=%s", mark_id, admin_id)
    return mark


async def stats(db: AsyncSession) -> dict:
    """概览页卡片：近 7 日问答量、待复核高危、已标记数。"""
    since = datetime.combine(date.today(), time.min)
    total_sessions = int(
        (await db.execute(select(func.count(ChatSession.id)))).scalar() or 0
    )
    total_messages = int(
        (
            await db.execute(
                select(func.count(Message.id)).where(
                    Message.role == "assistant", Message.created_at >= since
                )
            )
        ).scalar()
        or 0
    )
    marked = int(
        (
            await db.execute(
                select(func.count(ChatAuditMark.id)).where(ChatAuditMark.revoked_at.is_(None))
            )
        ).scalar()
        or 0
    )
    return {
        "sessions_total": total_sessions,
        "answers_today": total_messages,
        "marks_active": marked,
    }
