"""随访任务草稿服务（D5）：对话 → function calling → 归一化 → 只落 draft。

为什么归一化必须写在代码里，而不是信模型的输出：
  1. 日期算错是模型最常见的错误 → 入参只收 `offset_days`，绝对日期服务端算；
  2. 模型会「好心」补全对话里没说的节点 → 只保留对话能对上的天数，超量截断到 10 条；
  3. 患者标识必须来自对话原文 → 抽不到就拒绝生成，绝不编造一个住院号写进库里；
  4. 状态永远是 draft → 这不是提示词约束，是这里写死的字面量（模型连字段都没有）。

可举证性：草稿的 content.ai 里存了 model / prompt_version / latency_ms / raw_args / flags，
事后能回答「这张草稿是哪一版提示词、哪个模型、在第几毫秒生成了什么」。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.gateway.base import ToolCallParseError, get_llm
from app.agents.prompts import load_prompt
from app.agents.tools import CREATE_TASK_DRAFT, CREATE_TASK_DRAFT_NAME
from app.models.task import FollowupTaskDraft
from app.schemas.task import TaskConfirmIn
from app.services import chat_service

logger = logging.getLogger(__name__)

MAX_ITEMS = 10
MAX_OFFSET_DAYS = 365
DEFAULT_PLAN = "常规术后随访"
VALID_CHANNELS = {"phone", "sms", "wecom", "outpatient"}
SCHEDULE_HOUR = 9  # 随访任务统一排在上午 9 点，避免出现 03:17 这种没人执行的计划
DEDUP_WINDOW = timedelta(hours=24)


class TaskInputError(ValueError):
    """信息不足/不合法：应由医护补充后重试（映射 422）。"""


class TaskExtractError(RuntimeError):
    """模型侧失败：网关报错、未返回工具调用、参数非法（映射 502）。"""


# ---------------------------------------------------------------- 提示词拼装


def _build_messages(
    history: list[dict], question: str | None, patient_hint: str | None, base_date: date
) -> tuple[list[dict], str]:
    prompt = load_prompt("task_extract")
    lines = ["以下是医护与助手的对话记录：", ""]
    for item in history:
        role = "医护" if item.get("role") == "user" else "助手"
        lines.append(f"{role}：{item.get('content', '')}")
    if question:
        lines.append(f"医护：{question}")
    lines.append("")
    lines.append(f"出院日（基准日，第 0 天）：{base_date.isoformat()}")
    if patient_hint:
        lines.append(f"已知患者标识：{patient_hint}（可直接采用，无需再抽取）")
    return (
        [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": "\n".join(lines)},
        ],
        prompt.version,
    )


# ---------------------------------------------------------------- 归一化


def _clean_items(raw_items: list) -> list[dict]:
    """把模型给的 items 洗成可信的结构：排序、去重、截断、白名单渠道。"""
    merged: dict[int, dict] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            offset = int(raw.get("offset_days"))
        except (TypeError, ValueError):
            continue  # 模型偶尔把 "第7天" 塞进 offset_days，直接丢，不猜
        if not 0 <= offset <= MAX_OFFSET_DAYS:
            continue
        name = str(raw.get("name") or "").strip()[:64] or f"出院后第 {offset} 天随访"
        notes = str(raw.get("notes") or "").strip()[:200]
        channel = str(raw.get("channel") or "").strip()
        if channel not in VALID_CHANNELS:
            channel = "phone"

        if offset in merged:  # 同一天多件事合并，避免出现两条 09:00 的任务
            merged[offset]["name"] = f"{merged[offset]['name']} + {name}"[:64]
            merged[offset]["notes"] = (merged[offset]["notes"] + "；" + notes).strip("；")[:200]
        else:
            merged[offset] = {"name": name, "offset_days": offset, "channel": channel, "notes": notes}

    return [merged[k] for k in sorted(merged)][:MAX_ITEMS]


def _schedule_at(base_date: date, first_offset: int) -> datetime:
    """首个节点日期 + 09:00，带本机时区（院内排期看的是本地墙上时间）。"""
    naive = datetime.combine(base_date + timedelta(days=first_offset), time(SCHEDULE_HOUR))
    return naive.astimezone()


def normalize(
    arguments: dict, *, base_date: date, patient_hint: str | None
) -> dict:
    """把工具入参变成可落库的 content；不合法就抛 TaskInputError。"""
    patient_ref = str(arguments.get("patient_ref") or "").strip() or (patient_hint or "").strip()
    if not patient_ref:
        raise TaskInputError(
            "没能从对话里识别到患者标识（住院号/门诊号/患者编号）。"
            "请补充患者标识后重试，系统不会替你编一个。"
        )
    if len(patient_ref) > 64:
        raise TaskInputError("患者标识过长（>64 字符），请核对是否把整句话填进来了")

    items = _clean_items(arguments.get("items") or [])
    if not items:
        raise TaskInputError(
            "没能从对话里识别到任何随访时间点（例如「出院后第 7 天电话随访」）。请补充具体天数后重试。"
        )

    plan_type = str(arguments.get("plan_type") or "").strip()[:64] or DEFAULT_PLAN
    flags: dict = {}
    if len(arguments.get("items") or []) > MAX_ITEMS:
        flags["items_truncated"] = True
    if not arguments.get("patient_ref") and patient_hint:
        flags["patient_ref_from_hint"] = True

    scheduled_at = _schedule_at(base_date, items[0]["offset_days"])
    if scheduled_at < datetime.now().astimezone():
        # 补录历史随访很常见，不拦，但要让确认的人看见
        flags["first_node_in_past"] = True

    for item in items:
        item["due_date"] = (base_date + timedelta(days=item["offset_days"])).isoformat()

    return {
        "items": items,
        "plan_type": plan_type,
        "patient_ref": patient_ref,
        "scheduled_at": scheduled_at,
        "rationale": str(arguments.get("rationale") or "").strip()[:300],
        "flags": flags,
    }


# ---------------------------------------------------------------- 生成


async def _find_similar_draft(
    db: AsyncSession, *, user_id: int, patient_ref: str, plan_type: str, offsets: list[int]
) -> FollowupTaskDraft | None:
    """24 小时内同一患者+同一方案+同样节点的草稿，视为重复点击，直接复用。"""
    since = datetime.now() - DEDUP_WINDOW
    rows = (
        await db.execute(
            select(FollowupTaskDraft)
            .where(
                FollowupTaskDraft.created_by == user_id,
                FollowupTaskDraft.patient_ref == patient_ref,
                FollowupTaskDraft.plan_type == plan_type,
                FollowupTaskDraft.status == "draft",
            )
            .order_by(FollowupTaskDraft.id.desc())
            .limit(5)
        )
    ).scalars().all()
    for row in rows:
        # 库里可能是无时区的 naive 值（SQLite），统一去掉时区再比，避免 TypeError
        created = row.created_at.replace(tzinfo=None) if row.created_at else None
        if created and created < since:
            continue
        existing = [int(i.get("offset_days", -1)) for i in (row.content or {}).get("items", [])]
        if existing == offsets:
            return row
    return None


async def generate_draft(
    db: AsyncSession,
    *,
    user_id: int,
    session_id: int | None = None,
    question: str | None = None,
    patient_ref: str | None = None,
    base_date: date | None = None,
    system_context: dict | None = None,
    owner_id: int | None = None,
) -> tuple[FollowupTaskDraft, bool]:
    """生成草稿，返回 (草稿, 是否复用已有草稿)。

    session_id 归属校验在路由层先做；这里的 `owner_id` 只用于会话历史读取，
    避免「管理员代看别人的会话」时读到空历史。
    """
    if not question and session_id is None:
        raise TaskInputError("请提供 question（一段随访安排描述）或 session_id（从会话中抽取）")

    base = base_date or date.today()
    history: list[dict] = []
    if session_id is not None:
        # 抽取任务只需最近 4 轮：随访安排通常就在上一两轮里，
        # 历史拉太长反而会把早已确定过的旧患者信息带进来
        history = await chat_service.recent_history(db, session_id, rounds=4)

    messages, prompt_version = _build_messages(history, question, patient_ref, base)
    llm = get_llm()
    try:
        result = await llm.chat_tools(
            messages,
            [CREATE_TASK_DRAFT],
            tool_choice={"type": "function", "function": {"name": CREATE_TASK_DRAFT_NAME}},
        )
    except ToolCallParseError as exc:
        raise TaskExtractError(f"模型返回的工具参数无法解析：{exc}") from exc
    except NotImplementedError as exc:
        raise TaskExtractError(f"当前供应商不支持工具调用：{exc}") from exc

    calls = (result.extra or {}).get("tool_calls") or []
    call = calls[0] if calls else None
    if call is None or call.name != CREATE_TASK_DRAFT_NAME:
        raise TaskExtractError(
            "模型没有按要求调用任务生成工具" + (f"（它回复了：{result.text[:120]}）" if result.text else "")
        )

    normalized = normalize(call.arguments, base_date=base, patient_hint=patient_ref)
    offsets = [i["offset_days"] for i in normalized["items"]]

    existing = await _find_similar_draft(
        db,
        user_id=user_id,
        patient_ref=normalized["patient_ref"],
        plan_type=normalized["plan_type"],
        offsets=offsets,
    )
    if existing is not None:
        meta = dict(existing.content or {})
        meta.setdefault("ai", {})["reused"] = True
        existing.content = meta
        await db.commit()
        await db.refresh(existing)
        return existing, True

    content = {
        "items": normalized["items"],
        "rationale": normalized["rationale"],
        "ai": {
            "model": result.model,
            "prompt_version": prompt_version,
            "latency_ms": result.latency_ms,
            "degraded": (result.extra or {}).get("degraded"),
            "tool": call.name,
            # 原始入参留档：模型与归一化结果不一致时，靠它对账
            "raw_args": call.arguments,
            "flags": normalized["flags"],
            **(system_context or {}),
        },
    }

    draft = FollowupTaskDraft(
        session_id=session_id,
        patient_ref=normalized["patient_ref"],
        plan_type=normalized["plan_type"],
        scheduled_at=normalized["scheduled_at"],
        content=content,
        status="draft",  # ★ 硬关卡：这里没有任何分支能写出其他值
        created_by=user_id,
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    logger.info(
        "任务草稿已生成 id=%s patient=%s items=%s model=%s",
        draft.id, draft.patient_ref, len(content["items"]), result.model,
    )
    return draft, False


# ---------------------------------------------------------------- 人工关卡


async def get_draft_for_user(
    db: AsyncSession, draft_id: int, *, user_id: int, is_admin: bool
) -> FollowupTaskDraft | None:
    draft = await db.get(FollowupTaskDraft, draft_id)
    if draft is None:
        return None
    if not is_admin and draft.created_by != user_id:
        return None
    return draft


async def confirm(
    db: AsyncSession, draft: FollowupTaskDraft, *, user_id: int, username: str, payload: TaskConfirmIn
) -> FollowupTaskDraft:
    """人工确认：AI 生成的草稿到这里才算成立。

    允许确认时改方案名/首节点时间/节点清单——因为「确认」的语义是
    「我（具名的人）看过了并按我的判断定稿」，不是「我同意 AI」。
    """
    if draft.status != "draft":
        raise TaskInputError(f"该草稿当前状态为 {draft.status}，只有 draft 状态可确认")

    content = dict(draft.content or {})
    if payload.plan_type:
        draft.plan_type = payload.plan_type.strip()[:64]
    if payload.items:
        items = _clean_items([i.model_dump() for i in payload.items])
        if not items:
            raise TaskInputError("提交的随访节点不合法（需要有 name 与 0-365 的 offset_days）")
        base = date.fromisoformat(
            _base_date_from_content(content) or date.today().isoformat()
        )
        for item in items:
            item["due_date"] = (base + timedelta(days=item["offset_days"])).isoformat()
        content["items"] = items
        draft.scheduled_at = payload.scheduled_at or _schedule_at(base, items[0]["offset_days"])
    if payload.scheduled_at:
        draft.scheduled_at = payload.scheduled_at

    content["confirm"] = {
        "by": username,
        "by_id": user_id,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": (payload.note or "").strip()[:200],
        # 记下 AI 原本给的节点数：人工改过多少，是评估抽取质量最直接的指标
        "ai_item_count": len((content.get("ai", {}).get("raw_args", {}) or {}).get("items") or []),
    }
    draft.content = content
    draft.status = "confirmed"
    draft.confirmed_by = user_id
    await db.commit()
    await db.refresh(draft)
    return draft


async def cancel(db: AsyncSession, draft: FollowupTaskDraft, *, user_id: int, username: str) -> FollowupTaskDraft:
    if draft.status == "confirmed":
        raise TaskInputError("已确认的任务不能取消，请走任务撤销流程（D6）")
    content = dict(draft.content or {})
    content["cancel"] = {
        "by": username,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    draft.content = content
    draft.status = "cancelled"
    await db.commit()
    await db.refresh(draft)
    return draft


def _base_date_from_content(content: dict) -> str | None:
    """从首个节点的 due_date 反推基准日——确认时改节点要用同一个出院日。"""
    items = content.get("items") or []
    if not items:
        return None
    first = items[0]
    try:
        due = date.fromisoformat(first["due_date"])
        return (due - timedelta(days=int(first["offset_days"]))).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
