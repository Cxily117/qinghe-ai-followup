"""随访任务路由（D5）：AI 只出草稿，人点确认。

权限与状态机：
  POST /tasks                 登录用户    生成 draft（只能生成 draft）
  POST /tasks/{id}/confirm    草稿归属人   draft → confirmed（不可重复确认，409）
  POST /tasks/{id}/cancel     草稿归属人   draft → cancelled（已确认不可取消，409）
  GET  /tasks/{id}            归属人或管理员
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, DbDep
from app.models.task import FollowupTaskDraft
from app.schemas.task import TaskConfirmIn, TaskDraftGenerateIn, TaskDraftOut
from app.services import chat_service, task_service

router = APIRouter(prefix="/tasks", tags=["随访任务"])


def _out(draft: FollowupTaskDraft) -> dict:
    return TaskDraftOut(
        id=draft.id,
        session_id=draft.session_id,
        patient_ref=draft.patient_ref,
        plan_type=draft.plan_type,
        scheduled_at=draft.scheduled_at,
        status=draft.status,
        content=draft.content,
        confirmed_by=draft.confirmed_by,
        created_by=draft.created_by,
        created_at=draft.created_at,
    ).model_dump()


@router.get("/drafts", summary="我的随访任务草稿")
async def list_drafts(user: CurrentUser, db: DbDep, status_filter: str | None = None):
    stmt = select(FollowupTaskDraft).where(FollowupTaskDraft.created_by == user.id)
    if status_filter:
        stmt = stmt.where(FollowupTaskDraft.status == status_filter)
    rows = (await db.execute(stmt.order_by(FollowupTaskDraft.id.desc()).limit(50))).scalars().all()
    return [_out(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="从对话生成任务草稿（function calling）")
async def create_task(payload: TaskDraftGenerateIn, user: CurrentUser, db: DbDep):
    if payload.session_id is None and not (payload.question or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="请提供 question（一段随访安排描述）或 session_id（从会话中抽取）",
        )
    if payload.session_id is not None:
        if await chat_service.get_session_for_user(db, payload.session_id, user.id) is None:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    try:
        draft, reused = await task_service.generate_draft(
            db,
            user_id=user.id,
            session_id=payload.session_id,
            question=(payload.question or "").strip() or None,
            patient_ref=payload.patient_ref,
            base_date=payload.base_date,
            system_context={"trigger": "session" if payload.session_id else "manual"},
        )
    except task_service.TaskInputError as exc:
        # 信息不足：让医护补，不是服务器故障
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except task_service.TaskExtractError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"任务抽取失败：{exc}")

    data = _out(draft)
    data["reused"] = reused  # 24h 内同患者同节点重复提交：复用旧草稿，避免列表刷出重复卡片
    return data


@router.get("/{draft_id}", summary="草稿详情（含 AI 溯源）")
async def get_draft(draft_id: int, user: CurrentUser, db: DbDep):
    draft = await task_service.get_draft_for_user(
        db, draft_id, user_id=user.id, is_admin=user.role == "admin"
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在或无权访问")
    return _out(draft)


@router.post("/{draft_id}/confirm", summary="人工确认草稿（AI 无法调用）")
async def confirm_draft(draft_id: int, payload: TaskConfirmIn, user: CurrentUser, db: DbDep):
    draft = await task_service.get_draft_for_user(
        db, draft_id, user_id=user.id, is_admin=user.role == "admin"
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在或无权访问")
    if draft.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该草稿当前状态为 {draft.status}，只有 draft 状态可确认",
        )
    try:
        draft = await task_service.confirm(
            db, draft, user_id=user.id, username=user.username, payload=payload
        )
    except task_service.TaskInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _out(draft)


@router.post("/{draft_id}/cancel", summary="取消草稿")
async def cancel_draft(draft_id: int, user: CurrentUser, db: DbDep):
    draft = await task_service.get_draft_for_user(
        db, draft_id, user_id=user.id, is_admin=user.role == "admin"
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在或无权访问")
    try:
        draft = await task_service.cancel(db, draft, user_id=user.id, username=user.username)
    except task_service.TaskInputError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _out(draft)
