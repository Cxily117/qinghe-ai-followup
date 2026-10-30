"""智能问答路由：SSE 流式 + 非流式兜底 + 会话管理。

两个入口共用同一个编排服务（app/services/qa_service.py），保证回答品质一致：
  POST /chat       SSE 流式（前端主用）
  POST /chat/ask   一次性 JSON（curl 回归、脚本调用、流式不可用时的兜底）
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentUser, DbDep
from app.schemas.chat import AskRequest, AskResponse, MessageOut, SessionOut
from app.services import chat_service, qa_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["智能问答"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 Nginx 等反代的缓冲，否则流式会被攒成一整块再发，前端看不到"打字机"效果
    "X-Accel-Buffering": "no",
}


@router.post("", summary="提问（SSE 流式）")
async def chat_stream(payload: AskRequest, user: CurrentUser, db: DbDep):
    """事件流：session → citations → delta* → (replace) → done。

    先做一次会话归属校验：非法 session_id 直接 404，
    而不是先吐出 200 的流再在流里报错（前端处理起来更麻烦）。
    """
    if payload.session_id is not None:
        exists = await chat_service.get_session_for_user(db, payload.session_id, user.id)
        if exists is None:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    user_id = user.id  # 依赖注入的 db/user 在响应开始后会关闭，先把值取出来

    async def event_source():
        try:
            async for event in qa_service.stream_events(
                user_id=user_id,
                question=payload.question,
                session_id=payload.session_id,
            ):
                yield _sse(event)
        except Exception as exc:  # 兜底：流内异常也要以事件形式告知前端，而不是断连
            logger.exception("问答流异常")
            yield _sse({"type": "error", "detail": f"服务异常：{exc}"})
        yield _sse({"type": "end"})

    return StreamingResponse(event_source(), media_type="text/event-stream", headers=SSE_HEADERS)


def _sse(event: dict) -> str:
    """统一 SSE 帧格式：event 名 + JSON data（不转义中文，便于人工 curl 排查）。"""
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/ask", response_model=AskResponse, summary="提问（非流式，返回完整回答）")
async def chat_ask(payload: AskRequest, user: CurrentUser, db: DbDep):
    try:
        return await qa_service.answer_once(
            db, user_id=user.id, question=payload.question, session_id=payload.session_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # 上游模型失败：502，前端可提示"稍后重试"
        logger.exception("问答失败")
        raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}")


@router.post("/sessions", status_code=status.HTTP_201_CREATED, summary="新建会话")
async def create_session(user: CurrentUser, db: DbDep):
    session = await chat_service.create_session(db, user.id)
    return _session_out(session)


@router.get("/sessions", summary="我的会话列表")
async def list_sessions(user: CurrentUser, db: DbDep):
    rows = await chat_service.list_sessions(db, user.id)
    return [_session_out(r) for r in rows]


@router.get("/sessions/{session_id}/messages", summary="会话消息（含溯源 meta）")
async def list_messages(session_id: int, user: CurrentUser, db: DbDep):
    messages = await chat_service.list_messages(db, session_id, user.id)
    if messages is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            meta=m.meta,
            created_at=m.created_at.isoformat() if m.created_at else None,
        ).model_dump()
        for m in messages
    ]


def _session_out(session) -> dict:
    return SessionOut(
        id=session.id,
        title=session.title,
        status=session.status,
        created_at=session.created_at.isoformat() if session.created_at else None,
        updated_at=session.updated_at.isoformat() if session.updated_at else None,
    ).model_dump()
