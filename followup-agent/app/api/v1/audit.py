"""对话审计接口（管理员）。

audit 是只读 + 两种写操作（标记/撤销标记）。所有查询都强制 role='staff'，
管理员自己的问答不进审计池——自己审自己等于没审。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.core.deps import AdminUser, DbDep
from app.schemas.audit import MarkIn
from app.services import audit_service

router = APIRouter(prefix="/audit", tags=["对话审计（管理员）"])


@router.get("/users", summary="医护列表（筛选下拉）")
async def list_users(admin: AdminUser, db: DbDep):
    users = await audit_service.list_staff(db)
    return [
        {"id": u.id, "name": u.full_name or u.username, "department": u.department or ""}
        for u in users
    ]


@router.get("/sessions", summary="会话列表（按人/时间段/风险标签筛选）")
async def list_sessions(
    admin: AdminUser,
    db: DbDep,
    user_id: int | None = Query(default=None, description="医护 id"),
    date_from: date | None = Query(default=None, description="开始日期，含当天"),
    date_to: date | None = Query(default=None, description="结束日期，含当天"),
    only_flagged: bool = Query(default=False, description="只看自动预筛为疑似的会话"),
    only_marked: bool = Query(default=False, description="只看已人工标记的会话"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return await audit_service.query_sessions(
        db,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        only_flagged=only_flagged,
        only_marked=only_marked,
        page=page,
        page_size=page_size,
    )


@router.get("/sessions/{session_id}", summary="会话明细（消息 + flags + 引用 + 标记）")
async def session_detail(session_id: int, admin: AdminUser, db: DbDep):
    return await audit_service.session_detail(db, session_id)


@router.post("/sessions/{session_id}/marks", summary="标记疑似高危")
async def create_mark(session_id: int, payload: MarkIn, admin: AdminUser, db: DbDep):
    mark = await audit_service.add_mark(
        db,
        session_id=session_id,
        admin_id=admin.id,
        level=payload.level,
        note=payload.note,
        message_id=payload.message_id,
    )
    return {
        "id": mark.id,
        "session_id": mark.session_id,
        "level": mark.level,
        "note": mark.note,
        "created_at": mark.created_at,
        "message": "已标记；同一会话重复标记会返回原记录",
    }


@router.post("/marks/{mark_id}/revoke", summary="撤销标记（软撤回，留痕）")
async def revoke_mark(mark_id: int, admin: AdminUser, db: DbDep):
    mark = await audit_service.revoke_mark(db, mark_id=mark_id, admin_id=admin.id)
    return {
        "id": mark.id,
        "revoked_at": mark.revoked_at,
        "message": "已撤销标记（记录保留，撤销人已留痕）",
    }


@router.get("/stats", summary="审计概览统计")
async def audit_stats(admin: AdminUser, db: DbDep):
    return await audit_service.stats(db)
