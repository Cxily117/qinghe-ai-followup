"""随访任务路由：草稿列表已实现（验证表结构与权限），生成接口 D5 实现。"""
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, DbDep
from app.models.task import FollowupTaskDraft

router = APIRouter(prefix="/tasks", tags=["随访任务"])


@router.get("/drafts", summary="我的随访任务草稿")
async def list_drafts(user: CurrentUser, db: DbDep):
    """验证 followup_task_drafts 表可用 + 按用户隔离。"""
    rows = (
        await db.execute(
            select(FollowupTaskDraft)
            .where(FollowupTaskDraft.created_by == user.id)
            .order_by(FollowupTaskDraft.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "patient_ref": r.patient_ref,
            "plan_type": r.plan_type,
            "status": r.status,
            "scheduled_at": r.scheduled_at,
        }
        for r in rows
    ]


@router.post("", status_code=status.HTTP_501_NOT_IMPLEMENTED, summary="从对话生成任务草稿")
async def create_task(user: CurrentUser):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="任务生成（function calling）将在 D5 实现",
    )
