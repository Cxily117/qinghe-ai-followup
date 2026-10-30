"""D6 质控报表服务：把散在各表里的「质量痕迹」汇成可决策的指标。

设计口径（为什么这么算）：
  质控报表的价值取决于口径是否稳定——每个指标都能回答「分子是什么、分母是什么」。
  这里刻意全部基于**已落库的痕迹**（messages.meta / drafts.content / agent_tool_calls），
  不引入新的运行时打点：报表坏了可以重算，痕迹丢了就真的没了。

指标总览（六组）：
  1. qa        问答质量：无引用率、拒答率、护栏改写率、红旗命中、模型/提示词版本分布
  2. drafts    任务草稿：状态分布、确认率、AI 人工修改率（AI 节点 vs 人定稿节点）、复用率
  3. agent     智能体运行：状态分布、blocked 率（模型试图越权）、升级/转人工、平均轮数
  4. tools     工具调用 Top：次数、失败率、平均耗时（找慢工具/坏工具）
  5. confirms  确认单漏斗：pending/confirmed/cancelled/expired——闸门是否被人用起来
  6. marks     人工审计标记：高危/关注 计数（质检工作量的直接体现）

性能说明：messages.meta 的细粒度统计在 Python 侧聚合（JSON 字段跨 PG/SQLite
没有可移植的取值语法；院内数据规模下「窗口内 assistant 消息」是几千行量级，
一次取 meta 列聚合即可。数据量上来后再换成 PG jsonb 路径，口径不变）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentConfirmation, AgentRun, AgentToolCall
from app.models.audit import ChatAuditMark
from app.models.chat import Message
from app.models.task import FollowupTaskDraft

# 护栏改写的标志位：出现任何一个都说明「模型的原话被动过了」
GUARDRAIL_FLAG_KEYS = (
    "refusal",
    "dropped_citations",
    "uncited_answer",
    "suspected_general_knowledge",
    "medical_advice_suspected",
    "empty_kb",
)


def _pct(numerator: int, denominator: int) -> float:
    """百分比保留 1 位小数；分母为 0 返回 0.0（报表不出现 NaN）。"""
    return round(numerator / denominator * 100, 1) if denominator else 0.0


def _naive(dt: datetime | None) -> datetime | None:
    """SQLite 存 naive、PG 存 aware，统一去 tz 再比较（与 task_service 同一处理）。"""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


async def _qa_metrics(db: AsyncSession, since: datetime) -> dict:
    """问答质量：从 assistant 消息的 meta 里聚合。

    「无引用回答」单列而不算进拒答：拒答（refusal）是护栏的正当行为，
    而「给了内容却没引用」才是要盯的质量问题——它意味着模型可能在编。
    """
    rows = (
        await db.execute(
            select(Message.meta)
            .where(
                Message.role == "assistant",
                Message.meta.is_not(None),
                Message.created_at >= since,
            )
        )
    ).scalars().all()

    total = len(rows)
    refusal = no_citation = red_flag = rewritten = 0
    latencies: list[int] = []
    models: dict[str, int] = {}
    prompt_versions: dict[str, int] = {}

    for meta in rows:
        meta = meta or {}
        flags = meta.get("flags") or {}
        citations = meta.get("citations") or []

        if flags.get("refusal"):
            refusal += 1
        elif not citations:  # 拒答本来就没有引用，不计入无引用
            no_citation += 1
        if flags.get("red_flag_question"):
            red_flag += 1
        if any(flags.get(k) for k in GUARDRAIL_FLAG_KEYS):
            rewritten += 1

        if isinstance(meta.get("latency_ms"), int):
            latencies.append(meta["latency_ms"])
        model = meta.get("model") or "unknown"
        models[model] = models.get(model, 0) + 1
        version = meta.get("prompt_version") or "unknown"
        prompt_versions[version] = prompt_versions.get(version, 0) + 1

    return {
        "answers": total,
        "refusal": refusal,
        "refusal_rate": _pct(refusal, total),
        "no_citation": no_citation,
        "no_citation_rate": _pct(no_citation, total),
        "guardrail_rewritten": rewritten,
        "guardrail_rate": _pct(rewritten, total),
        "red_flag_questions": red_flag,
        "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1])[:5]),
        "prompt_versions": dict(
            sorted(prompt_versions.items(), key=lambda kv: -kv[1])[:5]
        ),
    }


async def _draft_metrics(db: AsyncSession, since: datetime) -> dict:
    """任务草稿：确认率 + AI 人工修改率。

    「人工修改率」口径：confirmed 草稿里，人工定稿的节点集合
    （content.items 的 offset_days）与 AI 原始输出（content.ai.raw_args）不一致。
    这比「数量不同」更准——人把第 7 天改成第 14 天、数量没变，也算修改。
    """
    rows = (
        await db.execute(
            select(FollowupTaskDraft).where(FollowupTaskDraft.created_at >= since)
        )
    ).scalars().all()

    total = len(rows)
    by_status = {"draft": 0, "confirmed": 0, "cancelled": 0}
    ai_total = reused = truncated = past_node = 0
    confirmed = modified = 0
    confirm_hours: list[float] = []
    plans: dict[str, int] = {}

    def _offsets(items: list | None) -> list[int]:
        out = []
        for i in items or []:
            try:
                out.append(int(i.get("offset_days")))
            except (TypeError, ValueError, AttributeError):
                continue
        return sorted(out)

    for d in rows:
        by_status[d.status] = by_status.get(d.status, 0) + 1
        content = d.content or {}
        ai = content.get("ai") or {}
        plans[d.plan_type or "未填"] = plans.get(d.plan_type or "未填", 0) + 1

        if ai:  # 有 ai 段 = AI 生成（人工直接新建的草稿没有）
            ai_total += 1
            if ai.get("reused"):
                reused += 1
            if (ai.get("flags") or {}).get("items_truncated"):
                truncated += 1
            if (ai.get("flags") or {}).get("first_node_in_past"):
                past_node += 1

        confirm = content.get("confirm")
        if confirm and d.status == "confirmed":
            confirmed += 1
            ai_offsets = _offsets((ai.get("raw_args") or {}).get("items"))
            human_offsets = _offsets(content.get("items"))
            if ai_offsets != human_offsets:
                modified += 1
            # 确认耗时：AI 生成 → 人确认的间隔（小时）。拖得越久说明草稿越难看懂
            try:
                made = _naive(datetime.fromisoformat(confirm["at"]))
                created = _naive(d.created_at)
                if made and created:
                    hours = (made - created).total_seconds() / 3600
                    if 0 <= hours < 24 * 30:  # 排除时钟回拨等脏数据
                        confirm_hours.append(hours)
            except (KeyError, ValueError, TypeError):
                pass

    return {
        "total": total,
        "by_status": by_status,
        "confirm_rate": _pct(by_status["confirmed"], total),
        "ai_generated": ai_total,
        "reused": reused,
        "confirmed": confirmed,
        "modified_by_human": modified,
        "modify_rate": _pct(modified, confirmed),
        "avg_confirm_hours": round(sum(confirm_hours) / len(confirm_hours), 1)
        if confirm_hours
        else 0.0,
        "items_truncated": truncated,
        "first_node_in_past": past_node,
        "plans": dict(sorted(plans.items(), key=lambda kv: -kv[1])[:5]),
    }


async def _agent_metrics(db: AsyncSession, since: datetime) -> dict:
    """智能体运行：过程质量（blocked 率、升级/转人工）比结果正确率更先恶化。"""
    runs = (
        await db.execute(
            select(AgentRun.status, AgentRun.steps, AgentRun.route, AgentRun.latency_ms).where(
                AgentRun.created_at >= since
            )
        )
    ).all()

    total = len(runs)
    status_dist: dict[str, int] = {}
    steps_list: list[int] = []
    latency_list: list[int] = []
    red_flag_runs = escalated_runs = 0

    for status, steps, route, latency in runs:
        status_dist[status] = status_dist.get(status, 0) + 1
        steps_list.append(steps or 0)
        if isinstance(latency, int):
            latency_list.append(latency)
        flags = (route or {}).get("flags") or []
        if "red_flag_high" in flags or "red_flag" in flags:
            red_flag_runs += 1
        if "escalated" in flags:
            escalated_runs += 1

    # blocked：模型调用了「当前状态下不可见」的工具——越权企图的直接计量
    blocked = int(
        (
            await db.execute(
                select(func.count(AgentToolCall.id))
                .join(AgentRun, AgentToolCall.run_id == AgentRun.id)
                .where(
                    AgentRun.created_at >= since,
                    AgentToolCall.decision == "blocked",
                )
            )
        ).scalar()
        or 0
    )
    tool_rows_total = int(
        (
            await db.execute(
                select(func.count(AgentToolCall.id))
                .join(AgentRun, AgentToolCall.run_id == AgentRun.id)
                .where(AgentRun.created_at >= since, AgentToolCall.decision == "tool_call")
            )
        ).scalar()
        or 0
    )

    confirms = (
        await db.execute(
            select(AgentConfirmation.status).where(AgentConfirmation.created_at >= since)
        )
    ).scalars().all()
    confirm_dist: dict[str, int] = {}
    for s in confirms:
        confirm_dist[s] = confirm_dist.get(s, 0) + 1

    return {
        "runs": total,
        "status_dist": status_dist,
        "avg_steps": round(sum(steps_list) / len(steps_list), 1) if steps_list else 0.0,
        "avg_latency_ms": int(sum(latency_list) / len(latency_list)) if latency_list else 0,
        "blocked_attempts": blocked,
        "blocked_rate": _pct(blocked, tool_rows_total),
        "red_flag_runs": red_flag_runs,
        "escalated_runs": escalated_runs,
        "confirmations": confirm_dist,
    }


async def _tool_metrics(db: AsyncSession, since: datetime) -> list[dict]:
    """工具调用 Top：调用次数、失败率、平均耗时（毫秒）。"""
    rows = (
        await db.execute(
            select(AgentToolCall.tool, AgentToolCall.ok, AgentToolCall.latency_ms)
            .join(AgentRun, AgentToolCall.run_id == AgentRun.id)
            .where(AgentRun.created_at >= since, AgentToolCall.decision == "tool_call")
        )
    ).all()

    stats: dict[str, dict] = {}
    for tool, ok, latency in rows:
        s = stats.setdefault(tool, {"calls": 0, "fail": 0, "latency_sum": 0})
        s["calls"] += 1
        if not ok:
            s["fail"] += 1
        if isinstance(latency, int):
            s["latency_sum"] += latency

    return [
        {
            "tool": tool,
            "calls": s["calls"],
            "fail_rate": _pct(s["fail"], s["calls"]),
            "avg_latency_ms": int(s["latency_sum"] / s["calls"]) if s["calls"] else 0,
        }
        for tool, s in sorted(stats.items(), key=lambda kv: -kv[1]["calls"])[:10]
    ]


async def _mark_metrics(db: AsyncSession, since: datetime) -> dict:
    """人工审计标记：质检工作量的直接体现（只统计未撤销的）。"""
    rows = (
        await db.execute(
            select(ChatAuditMark.level)
            .where(ChatAuditMark.created_at >= since, ChatAuditMark.revoked_at.is_(None))
        )
    ).scalars().all()
    dist: dict[str, int] = {}
    for level in rows:
        dist[level] = dist.get(level, 0) + 1
    return {"total": len(rows), "by_level": dist}


async def summary(db: AsyncSession, *, days: int = 30) -> dict:
    """汇总报表。days 由路由层约束在 1-365。"""
    since = datetime.now() - timedelta(days=days)
    return {
        "window": {
            "days": days,
            "since": since.replace(microsecond=0).isoformat(),
            "until": datetime.now().replace(microsecond=0).isoformat(),
        },
        "qa": await _qa_metrics(db, since),
        "drafts": await _draft_metrics(db, since),
        "agent": await _agent_metrics(db, since),
        "tools": await _tool_metrics(db, since),
        "marks": await _mark_metrics(db, since),
    }


def to_csv(data: dict) -> str:
    """把汇总摊平成两列 CSV（指标,值），给 Excel 质控台账用。

    嵌套 dict/list 用「组.键」或「组.键[序号]」展开，列名稳定可对账。
    """
    lines = ["指标,值"]

    def _emit(prefix: str, value) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                _emit(f"{prefix}.{k}", v)
        elif isinstance(value, list):
            for i, v in enumerate(value, 1):
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        _emit(f"{prefix}[{i}].{k2}", v2)
                else:
                    _emit(f"{prefix}[{i}]", v)
        else:
            # 值里带逗号会错列，全部包引号（CSV 标准转义）
            cell = str(value).replace('"', '""')
            lines.append(f'"{prefix}","{cell}"')

    for section, payload in data.items():
        _emit(section, payload)
    return "\n".join(lines) + "\n"
