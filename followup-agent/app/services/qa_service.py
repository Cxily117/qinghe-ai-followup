"""问答编排服务：检索 → 拼提示词 → 调 LLM → 护栏 → 落库。

为什么这一段要独立成服务而不是写在路由里：
  同一套链路要被「流式 /chat」和「非流式 /chat/ask」两个入口复用，
  而两者唯一的差别只是「边收边吐」还是「收完再返回」，其余逻辑必须一致——
  否则流式网关和兜底 JSON 接口的回答品质会漂移。

⚠️ 关于数据库会话的一个坑（FastAPI 0.106+ 行为）：
  带 yield 的依赖会在「响应开始发送前」就退出，所以流式生成器里【不能】用
  路由注入的 db 会话（那时它已经 close 了）。这里改为在生成器内部自建会话，
  生命周期与流对齐，写库一定发生在 finally 之前。
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import guardrails
from app.agents.gateway.base import get_llm
from app.agents.rag import build_messages, retrieve
from app.core.database import SessionLocal
from app.services import chat_service

logger = logging.getLogger(__name__)

# 知识库为空（或尚未发布任何文档）时的固定话术：不调模型，直接拒答——省一次费用
EMPTY_KB_ANSWER = "知识库中暂无已发布的随访制度文档，无法回答该问题。请联系管理员上传并发布制度后再试。"


def _meta(
    *,
    citations: list[int],
    hits_count: int,
    model: str,
    latency_ms: int,
    prompt_version: str,
    flags: dict,
    answer_chars: int,
    streamed: bool,
    citation_detail: list[dict] | None = None,
) -> dict:
    """assistant 消息的溯源元数据。字段命名与架构方案一致，便于后续质控报表统计。

    `citations` 只存编号（统计用），`citation_detail` 存完整引用对象（文件名+片段），
    后者是为了让前端在**切换会话回看历史**时也能渲染来源卡片——
    否则历史消息里只有 [1][2] 两个数字，医护看不出依据的是哪份制度。
    """
    return {
        "citations": citations,
        "citation_detail": citation_detail or [],
        "hits": hits_count,
        "model": model,
        "latency_ms": latency_ms,
        # 流式响应拿不到上游 usage，用字符数估算（标注 est_ 前缀，避免被当成真实计费数据）
        "answer_chars": answer_chars,
        "est_tokens": int(answer_chars / 1.6),
        "prompt_version": prompt_version,
        "flags": flags,
        "streamed": streamed,
    }


async def answer_once(
    db: AsyncSession, *, user_id: int, question: str, session_id: int | None = None
) -> dict:
    """非流式问答：一次返回完整结果（前端兜底与接口回归用）。"""
    started = time.perf_counter()

    session = await chat_service.get_or_create_session(db, user_id, session_id, question)
    history = await chat_service.recent_history(db, session.id)
    await chat_service.append_message(db, session.id, "user", question)

    hits = await retrieve(db, question)
    if not hits:
        latency_ms = int((time.perf_counter() - started) * 1000)
        message = await chat_service.append_message(
            db,
            session.id,
            "assistant",
            EMPTY_KB_ANSWER,
            meta=_meta(
                citations=[],
                hits_count=0,
                model="none",
                latency_ms=latency_ms,
                prompt_version="",
                flags={"empty_kb": True, "refusal": True},
                answer_chars=len(EMPTY_KB_ANSWER),
                streamed=False,
            ),
        )
        return {
            "session_id": session.id,
            "message_id": message.id,
            "answer": EMPTY_KB_ANSWER,
            "citations": [],
            "flags": {"empty_kb": True, "refusal": True},
            "model": "none",
            "latency_ms": latency_ms,
        }

    messages, prompt_version = build_messages(question, hits, history)
    llm = get_llm()
    result = await llm.chat(messages)

    answer, citations, flags = guardrails.apply_output_guardrails(result.text, len(hits))
    if guardrails.has_red_flag(question):
        flags["red_flag_question"] = True
    latency_ms = int((time.perf_counter() - started) * 1000)

    meta = _meta(
        citations=citations,
        hits_count=len(hits),
        model=result.model,
        latency_ms=latency_ms,
        prompt_version=prompt_version,
        flags=flags,
        answer_chars=len(answer),
        streamed=False,
        citation_detail=[h.to_citation(n) for n, h in enumerate(hits, 1) if n in citations],
    )
    meta["usage"] = result.usage
    message = await chat_service.append_message(
        db, session.id, "assistant", answer, meta=meta
    )

    return {
        "session_id": session.id,
        "message_id": message.id,
        "answer": answer,
        "citations": [h.to_citation(n) for n, h in enumerate(hits, 1) if n in citations],
        "flags": flags,
        "model": result.model,
        "latency_ms": latency_ms,
        "prompt_version": prompt_version,
    }


async def stream_events(
    *, user_id: int, question: str, session_id: int | None = None
):
    """流式问答：逐条 yield 事件字典，由路由层包成 SSE。

    事件类型：
      session   —— 会话 id（前端据此记住上下文）
      citations —— 检索命中的资料清单（回答开始前先给，便于前端展示「依据」）
      delta     —— 增量文本
      replace   —— 护栏改写了输出，前端用该文本整体覆盖（拒答去引用、越界引用剔除）
      done      —— 落库完成，含 message_id / 最终文本 / 引用 / flags
      error     —— 上游失败（已保存已生成的部分，便于复盘）
    """
    started = time.perf_counter()

    async with SessionLocal() as db:  # 自建会话，生命周期与流对齐
        try:
            session = await chat_service.get_or_create_session(
                db, user_id, session_id, question
            )
        except LookupError as exc:
            yield {"type": "error", "code": "SESSION_NOT_FOUND", "message": str(exc)}
            return

        # 历史要在写入本轮 user 消息之前取，否则会把当前问题重复拼两次
        history = await chat_service.recent_history(db, session.id)
        await chat_service.append_message(db, session.id, "user", question)
        yield {"type": "session", "session_id": session.id}

        hits = await retrieve(db, question)
        if not hits:
            latency_ms = int((time.perf_counter() - started) * 1000)
            message = await chat_service.append_message(
                db,
                session.id,
                "assistant",
                EMPTY_KB_ANSWER,
                meta=_meta(
                    citations=[],
                    hits_count=0,
                    model="none",
                    latency_ms=latency_ms,
                    prompt_version="",
                    flags={"empty_kb": True, "refusal": True},
                    answer_chars=len(EMPTY_KB_ANSWER),
                    streamed=True,
                ),
            )
            yield {"type": "delta", "text": EMPTY_KB_ANSWER}
            yield {
                "type": "done",
                "message_id": message.id,
                "final_text": EMPTY_KB_ANSWER,
                "citations": [],
                "flags": {"empty_kb": True, "refusal": True},
                "latency_ms": latency_ms,
            }
            return

        yield {"type": "citations", "citations": [h.to_citation(n) for n, h in enumerate(hits, 1)]}

        messages, prompt_version = build_messages(question, hits, history)
        llm = get_llm()
        model_name = getattr(llm, "model", llm.name)

        chunks: list[str] = []
        try:
            async for delta in llm.stream(messages):
                chunks.append(delta)
                yield {"type": "delta", "text": delta}
        except Exception as exc:  # noqa: BLE001 —— 上游任何异常都要落库留痕
            logger.exception("LLM 流式调用失败")
            partial = "".join(chunks)
            if partial:
                await chat_service.append_message(
                    db,
                    session.id,
                    "assistant",
                    partial,
                    meta=_meta(
                        citations=[],
                        hits_count=len(hits),
                        model=model_name,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        prompt_version=prompt_version,
                        flags={"stream_error": str(exc)[:200]},
                        answer_chars=len(partial),
                        streamed=True,
                    ),
                )
            yield {
                "type": "error",
                "code": "LLM_UPSTREAM",
                "message": f"模型调用失败：{exc}",
            }
            return

        raw = "".join(chunks)
        answer, citations, flags = guardrails.apply_output_guardrails(raw, len(hits))
        if guardrails.has_red_flag(question):
            flags["red_flag_question"] = True
        if answer != raw:
            # 护栏改写了输出（拒答去引用 / 剔除越界引用 / 追加免责），让前端整体覆盖
            yield {"type": "replace", "text": answer}

        latency_ms = int((time.perf_counter() - started) * 1000)
        message = await chat_service.append_message(
            db,
            session.id,
            "assistant",
            answer,
            meta=_meta(
                citations=citations,
                hits_count=len(hits),
                model=model_name,
                latency_ms=latency_ms,
                prompt_version=prompt_version,
                flags=flags,
                answer_chars=len(answer),
                streamed=True,
                citation_detail=[
                    h.to_citation(n) for n, h in enumerate(hits, 1) if n in citations
                ],
            ),
        )

        yield {
            "type": "done",
            "message_id": message.id,
            "final_text": answer,
            "citations": [h.to_citation(n) for n, h in enumerate(hits, 1) if n in citations],
            "flags": flags,
            "latency_ms": latency_ms,
            "model": model_name,
            "prompt_version": prompt_version,
        }
