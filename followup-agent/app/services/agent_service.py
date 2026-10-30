"""智能体编排：有界自主（bounded autonomy）的主循环。

和 qa_service 的区别就一句话：
  qa_service 是「先检索，再让模型解释这段检索结果」；
  agent_service 是「把检索、查患者、查已排节点这些能力交给模型，由它决定查什么、查几次」。

但自主是有边界的，边界全部落在代码里而不是提示词里：
  - 工具集按 `RunState` 动态下发，没解锁的模型看不见（`tools.available_specs`）；
  - 单轮步数上限 `agent_max_steps`，防死循环与费用失控；
  - 模型调用了未下发的工具 = 明确的"想跳步"信号，记 flag 并回灌"工具不可用"，
    而不是静默执行——这条是后面审计"它有没有试图跳过校验"的数据源。

SSE 事件（在原有问答事件之外新增两类）：
  run   —— 本次运行的 run_uid
  step  —— 工具步骤（start/ok/error），前端据此画时间线
回答仍然是 delta / replace / done，前端渲染逻辑不用重写。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import AsyncIterator

from app.agents import handlers
from app.agents.gateway.base import get_llm
from app.agents.guardrails import apply_output_guardrails
from app.agents.runtime import RunState, ticket_digest
from app.agents.tools import P1_ENABLED, available_specs
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.agent import AgentRun, AgentToolCall
from app.services import chat_service, prompt_service

logger = logging.getLogger(__name__)

# 工具名 → 执行体。P1 只有只读能力；写工具在 P2 接入，届时在这里注册即可。
_HANDLERS = {
    "resolve_patient": handlers.resolve_patient,
    "search_policy": handlers.search_policy,
    "list_existing_tasks": handlers.list_existing_tasks,
    "handoff_to_human": handlers.handoff_to_human,
}

MAX_STEPS_ANSWER = (
    "这轮处理步骤过多，已停止自动处理并转交人工。"
    "为避免出错，请人工确认随访安排后再录入。"
)


def _tool_message(call_id: str, payload: dict) -> dict:
    """工具结果回灌给模型的格式（OpenAI 兼容）。"""
    return {
        "role": "tool",
        "tool_call_id": call_id or "call_0",
        "content": json.dumps(payload, ensure_ascii=False)[: get_settings().agent_tool_result_chars * 4],
    }


def _digest(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return _mask_ids(text)[:250]


async def stream_agent_events(
    *, user_id: int, question: str, session_id: int | None = None
) -> AsyncIterator[dict]:
    started = time.perf_counter()
    s = get_settings()

    async with SessionLocal() as db:  # 流式响应必须自建会话：yield 依赖会提前退出
        try:
            session = await chat_service.get_or_create_session(
                db, user_id, session_id, question
            )
        except LookupError as exc:
            yield {"type": "error", "code": "SESSION_NOT_FOUND", "message": str(exc)}
            return

        # 历史要在写入本轮 user 消息之前取，否则当前问题会被拼两次
        history = await chat_service.recent_history(db, session.id)
        await chat_service.append_message(db, session.id, "user", question)

        run_uid = secrets.token_hex(8)
        run = AgentRun(
            run_uid=run_uid,
            session_id=session.id,
            user_id=user_id,
            mode=s.agent_mode,
            question=question[:2000],
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        yield {"type": "session", "session_id": session.id}
        yield {"type": "run", "run_uid": run_uid, "mode": s.agent_mode}

        state = RunState(
            run_uid=run_uid,
            user_id=user_id,
            session_id=session.id,
            mode=s.agent_mode,
            question=question,
        )
        prompt = await prompt_service.get_active(db, "agent_supervisor")
        messages: list[dict] = (
            [{"role": "system", "content": prompt.text}] + history + [{"role": "user", "content": question}]
        )

        llm = get_llm()
        seq = 0
        final_text = ""
        model_name = getattr(llm, "model", s.llm_chat_model)
        status = "succeeded"

        try:
            for _ in range(s.agent_max_steps):
                specs = available_specs(state, enabled=P1_ENABLED)
                if not specs:  # 理论上 handoff 永远在，兜底防死循环
                    break

                result = await llm.chat_tools(messages, [sp.schema for sp in specs])
                model_name = result.model or model_name
                calls = result.extra.get("tool_calls") or []

                if not calls:  # 模型给出最终答复
                    final_text = (result.text or "").strip()
                    break

                call = calls[0]  # 一轮只执行一个：顺序约束要求「一步一验证」，并发会打乱因果
                seq += 1
                spec = next((sp for sp in specs if sp.name == call.name), None)

                if spec is None:
                    # ★ 模型调了一个它不该看见的工具：这就是「试图跳步」的证据
                    state.flags.add("blocked_tool_call")
                    payload = {
                        "ok": False,
                        "error": "tool_unavailable",
                        "message": (
                            f"工具 {call.name} 当前不可用：前置校验未完成或该能力未开放。"
                            "请按标准顺序先完成校验；若无法完成，请调用 handoff_to_human。"
                        ),
                    }
                    messages.append(
                        {
                            "role": "assistant",
                            "content": result.text or "",
                            "tool_calls": [
                                {
                                    "id": call.call_id or f"call_{seq}",
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": call.raw_arguments or "{}",
                                    },
                                }
                            ],
                        }
                    )
                    messages.append(_tool_message(call.call_id, payload))
                    yield {
                        "type": "step",
                        "seq": seq,
                        "tool": call.name,
                        "status": "blocked",
                        "label": "已拦截越权调用",
                        "detail": {"reason": "tool_unavailable"},
                    }
                    db.add(
                        AgentToolCall(
                            run_id=run.id,
                            seq=seq,
                            tool=call.name,
                            args_redacted={"blocked": True},
                            ok=False,
                            error_code="TOOL_UNAVAILABLE",
                            result_digest=_digest(payload),
                        )
                    )
                    await db.commit()
                    continue

                yield {
                    "type": "step",
                    "seq": seq,
                    "tool": call.name,
                    "status": "start",
                    "label": f"正在{spec.label or call.name}…",
                }

                t0 = time.perf_counter()
                try:
                    payload = await _HANDLERS[call.name](state, call.arguments, db)
                except Exception as exc:  # 工具内部异常不该整轮崩掉：回灌错误让模型换路
                    logger.warning("工具 %s 执行失败：%s", call.name, exc)
                    payload = {"ok": False, "error": "tool_failed", "message": str(exc)[:200]}
                cost_ms = int((time.perf_counter() - t0) * 1000)
                state.touch(call.name)

                messages.append(
                    {
                        "role": "assistant",
                        "content": result.text or "",
                        "tool_calls": [
                            {
                                "id": call.call_id or f"call_{seq}",
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.raw_arguments or "{}",
                                },
                            }
                        ],
                    }
                )
                messages.append(_tool_message(call.call_id, payload))

                db.add(
                    AgentToolCall(
                        run_id=run.id,
                        seq=seq,
                        tool=call.name,
                        args_redacted=_redact_args(call.arguments),
                        ok=payload.get("ok", True) is not False,
                        error_code=str(payload.get("error") or ""),
                        result_digest=_digest(payload),
                        ticket_id=ticket_digest(state.rule_ticket or state.risk_ticket),
                        latency_ms=cost_ms,
                    )
                )
                await db.commit()

                yield {
                    "type": "step",
                    "seq": seq,
                    "tool": call.name,
                    "status": "ok" if payload.get("ok", True) is not False else "error",
                    "label": f"{spec.label}完成",
                    # 只给摘要，不把整段制度条文推进前端
                    "detail": _step_detail(call.name, payload),
                }
            else:
                # for-else：步数用尽仍未给出最终答复
                status = "max_steps"
                final_text = MAX_STEPS_ANSWER
                state.flags.add("max_steps")

        except Exception as exc:
            logger.exception("智能体运行异常")
            status = "failed"
            latency_ms = int((time.perf_counter() - started) * 1000)
            run.status, run.latency_ms, run.error_code = status, latency_ms, "AGENT_FAILED"
            run.steps, run.route = seq, state.route_digest()
            await db.commit()
            yield {"type": "error", "code": "AGENT_FAILED", "message": f"智能体处理失败：{exc}"}
            return

        # ---- 收口：护栏 + 落库 ----
        if not final_text:
            status = "handoff"
            final_text = "这个问题我无法在制度范围内处理，已转交人工，请稍候。"

        answer, citations, flags = apply_output_guardrails(final_text, len(state.citations))
        merged_flags = dict(flags)
        merged_flags.update({f: True for f in state.flags})
        merged_flags["tool_steps"] = seq
        latency_ms = int((time.perf_counter() - started) * 1000)

        citation_detail = [c for c in state.citations if c["n"] in citations]

        # ★ 智能体的最终答案此前没有流式吐过字，这里只发一次：
        #   护栏改写过 → replace（整体覆盖，虽然此刻屏上无残留，但语义正确）；
        #   未改写    → delta（走前端帧队列打字机，体验更好）。
        #   不能两个都发 —— 前端 delta 是追加语义，连发会把整段答案显示两遍。
        yield {"type": "citations", "citations": citation_detail}
        if answer != final_text:
            yield {"type": "replace", "text": answer}
        else:
            yield {"type": "delta", "text": answer}

        message = await chat_service.append_message(
            db,
            session.id,
            "assistant",
            answer,
            meta={
                "citations": citations,
                "citation_detail": citation_detail,
                "model": model_name,
                "latency_ms": latency_ms,
                "answer_chars": len(answer),
                "prompt_version": prompt.version_tag,
                "flags": merged_flags,
                "streamed": False,
                "agent": {
                    "run_uid": run_uid,
                    "mode": s.agent_mode,
                    "steps": seq,
                    "route": state.route,
                },
            },
        )

        run.status = status
        run.steps = seq
        run.route = state.route_digest()
        run.prompt_version = prompt.version_tag
        run.answer_chars = len(answer)
        run.latency_ms = latency_ms
        await db.commit()

        yield {
            "type": "done",
            "message_id": message.id,
            "final_text": answer,
            "citations": citation_detail,
            "flags": merged_flags,
            "latency_ms": latency_ms,
            "run_uid": run_uid,
            "steps": seq,
            "route": state.route,
        }


def _redact_args(args: dict) -> dict:
    """审计表存脱敏入参。

    分两类处理：
      - 患者标识/症状原文 → 打码（这是 PHI，审计表不该成为第二个泄露面）；
      - 检索词/原因 → 保留（脱敏后就无法复盘"它到底查了什么"），但截断。
    """
    masked_keys = {"patient_ref", "patient_id", "symptoms", "name"}
    keep_keys = {"query", "reason", "plan_type"}
    out: dict = {}
    for k, v in (args or {}).items():
        if isinstance(v, list):
            out[k] = f"[{len(v)} items]"
        elif isinstance(v, str):
            if k in masked_keys:
                out[k] = handlers.redact_arg(v)
            elif k in keep_keys:
                # 保留是为了复盘「它到底查了什么」，但 query 可能是模型复述的
                # 完整问题（内嵌住院号），必须先过 ID 掩码再截断。
                out[k] = _mask_ids(v)[:60]
            else:
                out[k] = _mask_ids(v)[:40]
        else:
            out[k] = v
    return out


# 院内标识的样子：ZY2026001 / P123456 / A0000123
_ID_RE = re.compile(r"\b[A-Za-z]{1,3}\d{5,}\b")


def _mask_ids(text: str) -> str:
    """把文本里的院内编号打码，用于结果摘要落库。"""
    return _ID_RE.sub(lambda m: m.group(0)[:2] + "*" * (len(m.group(0)) - 2), text)


def _step_detail(tool: str, payload: dict) -> dict:
    """给前端时间线的短摘要（不含条文全文）。"""
    if tool == "resolve_patient":
        return {
            "found": payload.get("found", False),
            "patient": payload.get("patient_display") or "",
            "dept": payload.get("dept") or "",
        }
    if tool == "search_policy":
        return {"hits": payload.get("count", 0)}
    if tool == "list_existing_tasks":
        return {"existing": payload.get("count", 0)}
    if tool == "handoff_to_human":
        return {"reason": payload.get("reason", "")[:60]}
    return {}
