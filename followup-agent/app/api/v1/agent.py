"""自主随访智能体路由（阶段 4）。

刻意与 `/chat` 并存而不是替换它：
  - `/chat` 是「固定流程 RAG」，链路短、延迟低、可解释，适合绝大多数制度问答；
  - `/agent/chat` 是「会调工具的智能体」，能查患者、能多次检索、能判合规，
    但延迟更高、路径不固定。
  保留两条路，出现问题可以一键切回，也便于做 A/B 对比「智能体到底比固定流程好在哪」。

`/agent/runs/{uid}` 是给审计用的：逐轮回放调了什么工具、带了哪张票据、返回什么摘要。
医护本人可看自己的；管理员可看全部。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.core.deps import CurrentUser, DbDep
from app.core.errors import AppError
from app.schemas.chat import AskRequest
from app.services import agent_service, chat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["自主智能体"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat", summary="智能体问答（SSE，含工具步骤事件）")
async def agent_chat(payload: AskRequest, user: CurrentUser, db: DbDep):
    s = get_settings()
    if not s.agent_enabled:
        raise AppError("AGENT_DISABLED")

    if payload.session_id is not None:
        exists = await chat_service.get_session_for_user(db, payload.session_id, user.id)
        if exists is None:
            raise AppError("SESSION_NOT_FOUND")

    user_id = user.id  # yield 依赖在响应开始后会退出，先把值取出来

    async def event_source():
        try:
            async for event in agent_service.stream_agent_events(
                user_id=user_id, question=payload.question, session_id=payload.session_id
            ):
                yield _sse(event)
        except Exception as exc:
            logger.exception("智能体流异常")
            yield _sse({"type": "error", "code": "AGENT_FAILED", "message": f"服务异常：{exc}"})
        yield _sse({"type": "end"})

    return StreamingResponse(event_source(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/runs/{run_uid}", summary="运行回放（工具调用明细）")
async def get_run(run_uid: str, user: CurrentUser, db: DbDep):
    from sqlalchemy import select

    from app.models.agent import AgentRun, AgentToolCall

    run = (
        await db.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))
    ).scalar_one_or_none()
    if run is None:
        raise AppError("AGENT_RUN_NOT_FOUND")
    # staff 只能看自己的运行记录；admin 可看全部（审计场景）
    if run.user_id != user.id and user.role != "admin":
        raise AppError("AGENT_RUN_NOT_FOUND")  # 404 不暴露存在性

    calls = (
        await db.execute(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run.id)
            .order_by(AgentToolCall.seq.asc())
        )
    ).scalars().all()

    return {
        "run_uid": run.run_uid,
        "status": run.status,
        "mode": run.mode,
        "steps": run.steps,
        "route": run.route or {},
        "prompt_version": run.prompt_version,
        "latency_ms": run.latency_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "tool_calls": [
            {
                "seq": c.seq,
                "tool": c.tool,
                "ok": c.ok,
                "error_code": c.error_code,
                "args_redacted": c.args_redacted or {},
                "result_digest": c.result_digest,
                "ticket": c.ticket_id,
                "latency_ms": c.latency_ms,
            }
            for c in calls
        ],
    }
