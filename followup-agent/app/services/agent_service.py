"""智能体编排：把 Harness 的循环接到 SSE、审计表与人工确认闸门上。

分层（改一处不影响另一处）：
  - `app/agents/harness.py`  —— 纯循环：决策 → 工具 → 回填 → 再决策，最多 5 轮；
  - `app/agents/handlers.py` —— 工具的真实执行体（查患者/检索/校验/挂起草稿）；
  - **本文件** —— 持久化与传输：每轮落 agent_tool_calls、把事件翻成 SSE、
    写操作挂起时落 agent_confirmations、收口时写 messages.meta。

SSE 事件（原有问答事件之外）：
  run              本次运行 run_uid
  step             工具步骤（start/ok/error/blocked），前端画时间线
  confirm_required ★ 写操作被闸门拦下：下发草稿内容，等医护点确认
回答仍然是 delta / replace / done，前端渲染逻辑不用重写。
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import AsyncIterator

from app.agents import handlers
from app.agents.gateway.base import get_llm
from app.agents.guardrails import apply_output_guardrails
from app.agents.harness import AgentHarness, RoundRecord, digest_of
from app.agents.runtime import RunState
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import AppError
from app.models.agent import AgentConfirmation, AgentRun, AgentToolCall
from app.services import chat_service, prompt_service

logger = logging.getLogger(__name__)

# 工具名 → 执行体。8 个工具全部在这里注册；
# 能不能被模型看见由 runtime/tools 的解锁条件决定，不在这里裁剪。
_HANDLERS = {
    "resolve_patient": handlers.resolve_patient,
    "search_policy": handlers.search_policy,
    "list_existing_tasks": handlers.list_existing_tasks,
    "assess_red_flag": handlers.assess_red_flag,
    "check_followup_rule": handlers.check_followup_rule,
    "create_followup_task_draft": handlers.create_followup_task_draft,
    "escalate_to_clinician": handlers.escalate_to_clinician,
    "handoff_to_human": handlers.handoff_to_human,
}

# 写操作：票据指纹要单独记，复盘时能对上"这张草稿是拿哪张票建的"
_WRITE_TOOLS = {"create_followup_task_draft", "escalate_to_clinician"}


def _ticket_for_tool(state: RunState, tool: str) -> str:
    if tool == "escalate_to_clinician":
        return state.risk_ticket or state.rule_ticket
    if tool == "create_followup_task_draft":
        return state.rule_ticket
    return ""


async def _persist_round(db, run_id: int, record: RoundRecord, state: RunState) -> None:
    """一轮决策落一行日志：工具名、参数、耗时、结果摘要、票据指纹、模型原话。

    这是"审计分析"的数据源：可以按 tool 统计失败率、按 decision=blocked
    统计"模型试图跳步"的频率、按 latency_ms 找慢工具。
    """
    db.add(
        AgentToolCall(
            run_id=run_id,
            seq=record.round_no,
            decision=record.decision,
            tool=record.tool or "(final)",
            args_redacted=record.args_redacted,
            model_note=record.model_note,
            ok=record.ok,
            error_code=record.error_code,
            result_digest=record.result_digest,
            ticket_id=digest_of(_ticket_for_tool(state, record.tool))
            if record.tool in _WRITE_TOOLS
            else "",
            latency_ms=record.latency_ms,
        )
    )
    await db.commit()


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
            [{"role": "system", "content": prompt.text}]
            + history
            + [{"role": "user", "content": question}]
        )

        llm = get_llm()
        model_name = getattr(llm, "model", s.llm_chat_model)
        status = "succeeded"
        final_text = ""
        confirmation: AgentConfirmation | None = None

        async def dispatch(name: str, args: dict) -> tuple[dict, int]:
            """工具分发：只做路由，业务全在 handlers 里。"""
            handler = _HANDLERS.get(name)
            if handler is None:
                return {"ok": False, "error": "tool_unavailable"}, 0
            t0 = time.perf_counter()
            payload = await handler(state, args, db)
            return payload, int((time.perf_counter() - t0) * 1000)

        harness = AgentHarness(
            llm=llm,
            state=state,
            messages=messages,
            dispatch=dispatch,
            max_rounds=s.agent_max_steps,
            mode=s.agent_mode,
            tool_result_chars=s.agent_tool_result_chars,
            model_name=model_name,
        )

        try:
            async for event in harness.run():
                kind = event.get("type")

                if kind == "round":
                    record = event["record"]
                    # 先把日志落了再推事件：宁可前端慢 5ms，也不要出现"界面走完、库里没有"
                    await _persist_round(db, run.id, record, state)
                    continue

                if kind == "step":
                    yield event
                    continue

                if kind == "confirm_required":
                    # ★ 写操作挂起：草稿内容存确认单，run 置 pending_confirm。
                    #   这一刻起模型不再有机会"自己确认"，只有人调 /confirm 能落库。
                    payload = harness.outcome.pending or {}
                    normalized = payload.get("normalized") or {}
                    confirmation = AgentConfirmation(
                        run_id=run.id,
                        payload={
                            **normalized,
                            "preview": payload.get("draft") or {},
                            "ai": {
                                "run_uid": run_uid,
                                "model": model_name,
                                "prompt_version": prompt.version_tag,
                            },
                        },
                        patient_ref=str(normalized.get("patient_ref") or ""),
                        plan_type=str(normalized.get("plan_type") or ""),
                        rule_sha=str(payload.get("rule_sha") or ""),
                        items_count=len(normalized.get("items") or []),
                        expires_at=datetime.now() + timedelta(seconds=s.agent_confirm_ttl),
                    )
                    db.add(confirmation)
                    await db.commit()
                    await db.refresh(confirmation)

                    yield {
                        "type": "confirm_required",
                        "confirmation_id": confirmation.id,
                        "run_uid": run_uid,
                        "draft": event.get("draft") or {},
                        "rule_sha": event.get("rule_sha") or "",
                        "expires_in": s.agent_confirm_ttl,
                        "note": "确认后才会写入随访任务草稿；取消则什么都不留。",
                    }
                    continue
        except Exception as exc:
            logger.exception("智能体运行异常")
            status = "failed"
            latency_ms = int((time.perf_counter() - started) * 1000)
            run.status, run.latency_ms, run.error_code = status, latency_ms, "AGENT_FAILED"
            run.steps, run.route = len(harness.outcome.rounds), state.route_digest()
            await db.commit()
            yield {"type": "error", "code": "AGENT_FAILED", "message": f"智能体处理失败：{exc}"}
            return

        outcome = harness.outcome
        status = outcome.status
        final_text = outcome.final_text
        model_name = harness.model_name or model_name
        pending_confirm = status == "pending_confirm"

        # ---- 收口：护栏 + 落库 ----
        if not final_text:
            status = "handoff"
            final_text = "这个问题我无法在制度范围内处理，已转交人工，请稍候。"

        if pending_confirm:
            # 待确认的提示语不是"制度回答"，不该被引用护栏改成拒答/免责话术
            answer, citations, flags = final_text, [], {}
        else:
            answer, citations, flags = apply_output_guardrails(
                final_text, len(state.citations)
            )

        merged_flags = dict(flags)
        merged_flags.update({f: True for f in state.flags})
        merged_flags["tool_steps"] = len(outcome.rounds)
        if pending_confirm:
            merged_flags["awaiting_confirm"] = True

        latency_ms = int((time.perf_counter() - started) * 1000)
        citation_detail = [c for c in state.citations if c["n"] in citations]

        # 智能体的最终答案此前没有流式吐过字，这里只发一次：
        #   护栏改写过 → replace；未改写 → delta（走前端打字机）。
        # 两个都发会把整段答案显示两遍（delta 是追加语义）。
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
                    "steps": len(outcome.rounds),
                    "route": state.route,
                    "confirm": {
                        "id": confirmation.id,
                        "status": "pending",
                    }
                    if confirmation
                    else None,
                },
            },
        )

        run.status = status
        run.steps = len(outcome.rounds)
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
            "steps": len(outcome.rounds),
            "route": state.route,
            "status": status,
            "awaiting_confirm": pending_confirm,
        }


# ---------------------------------------------------------------- 人工确认闸门


async def decide_confirmation(
    db,
    *,
    run_uid: str,
    user_id: int,
    username: str,
    is_admin: bool,
    action: str,
    note: str = "",
) -> dict:
    """医护对「待确认草稿」做决定：confirm = 落库成 draft，cancel = 什么都不留。

    这个函数是**唯一**能写出 FollowupTaskDraft 的智能体路径。模型没有 HTTP 能力，
    它的循环在产出 confirm_required 时就已终止——"自己确认自己"在结构上不可能。

    状态机：pending → confirmed（写库，终态）/ cancelled（终态）/ expired（超时）。
    """
    from sqlalchemy import select

    from app.models.task import FollowupTaskDraft

    run = (
        await db.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))
    ).scalar_one_or_none()
    if run is None:
        raise AppError("AGENT_RUN_NOT_FOUND")
    if run.user_id != user_id and not is_admin:
        # 404 不暴露"存在但不是你的"
        raise AppError("AGENT_RUN_NOT_FOUND")
    if run.status != "pending_confirm":
        raise AppError("AGENT_CONFIRM_CLOSED", f"该运行当前状态为 {run.status}，没有待确认的草稿")

    confirmation = (
        await db.execute(
            select(AgentConfirmation)
            .where(
                AgentConfirmation.run_id == run.id,
                AgentConfirmation.status == "pending",
            )
            .order_by(AgentConfirmation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if confirmation is None:
        raise AppError("AGENT_CONFIRM_NOT_FOUND")

    now = datetime.now()
    expires = confirmation.expires_at
    # PG 返回 tz-aware、SQLite 返回 naive：比较前先对齐，避免 TypeError
    if expires and expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if expires and now > expires:
        # 过期不自动落库：按旧制度算出来的方案，隔夜确认风险太高
        confirmation.status = "expired"
        run.status = "expired"
        await db.commit()
        raise AppError("AGENT_CONFIRM_EXPIRED")

    if action == "cancel":
        confirmation.status = "cancelled"
        confirmation.decided_at = now
        confirmation.decided_by = user_id
        run.status = "cancelled"
        await db.commit()
        await chat_service.append_message(
            db,
            run.session_id,
            "assistant",
            "已取消，本次没有写入任何随访任务草稿。",
            meta={"agent": {"run_uid": run_uid, "confirm": {"id": confirmation.id, "status": "cancelled"}}},
        )
        return {"run_uid": run_uid, "status": "cancelled", "confirmation_id": confirmation.id}

    # ---- confirm：从确认单里取参数落库 ----
    payload = dict(confirmation.payload or {})
    items = payload.get("items") or []
    if not items:
        raise AppError("AGENT_CONFIRM_NOT_FOUND", "确认单里没有可落库的随访节点，请重新生成")

    scheduled_at = payload.get("scheduled_at")
    parsed_at = None
    if isinstance(scheduled_at, str):
        try:
            parsed_at = datetime.fromisoformat(scheduled_at)
        except ValueError:
            parsed_at = None

    content = {
        "items": items,
        "rationale": payload.get("rationale", ""),
        "plan_type": payload.get("plan_type", ""),
        "ai": {
            **(payload.get("ai") or {}),
            "source": "agent",
            "rule_sha": confirmation.rule_sha,
            "confirmed_via": "agent_confirm_card",
        },
        "confirm": {
            # 注意：这里不是任务确认，只是"草稿已由人过目后落库"
            "draft_saved_by": username,
            "draft_saved_by_id": user_id,
            "at": now.astimezone().isoformat(timespec="seconds"),
            "note": (note or "").strip()[:200],
        },
    }

    draft = FollowupTaskDraft(
        session_id=run.session_id,
        patient_ref=confirmation.patient_ref,
        plan_type=confirmation.plan_type or payload.get("plan_type", ""),
        scheduled_at=parsed_at,
        content=content,
        # ★ 落库仍然是 draft：智能体只能到"待人工确认的草稿"，
        #   任务层面的 confirmed 永远要在任务列表里由人再点一次。
        status="draft",
        created_by=run.user_id,
    )
    db.add(draft)
    await db.flush()

    confirmation.status = "confirmed"
    confirmation.draft_id = draft.id
    confirmation.decided_at = now
    confirmation.decided_by = user_id
    run.status = "confirmed"
    await db.commit()
    await db.refresh(draft)

    await chat_service.append_message(
        db,
        run.session_id,
        "assistant",
        f"已保存为随访任务草稿 #{draft.id}（{len(items)} 个节点）。"
        "请在「任务草稿」中核对后再点确认，AI 不会替你完成确认。",
        meta={
            "agent": {
                "run_uid": run_uid,
                "confirm": {"id": confirmation.id, "status": "confirmed", "draft_id": draft.id},
            }
        },
    )
    return {
        "run_uid": run_uid,
        "status": "confirmed",
        "confirmation_id": confirmation.id,
        "draft_id": draft.id,
        "items_count": len(items),
    }
