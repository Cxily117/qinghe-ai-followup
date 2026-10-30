"""工具的服务端执行体。

原则：**工具返回的是「事实」，不是「结论」**。
  - `resolve_patient` 只返回院内的患者记录，查不到就 found=false，绝不替模型猜；
  - `search_policy` 只返回条文原文片段，不替模型判断"这算不算合规"；
  - `check_followup_rule` 的 passed 由规则引擎算（`rules.py`），模型无权自判；
  - `create_followup_task_draft` **不落库**，只生成一张待确认单——
    "确认"是人的动作，不是模型的一轮对话。

这一层是"可举证"的落点：模型说的每句话要么来自这里的返回，要么就是它编的。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import rules
from app.agents.rag import retrieve
from app.agents.runtime import (
    RunState,
    consume_ticket,
    issue_ticket,
    verify_ticket,
)
from app.core.errors import AppError
from app.models.task import FollowupTaskDraft
from app.services import task_service

DATA_DIR = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def _patients() -> list[dict]:
    """演示用患者目录（模拟 EMPI）。

    真实部署时这里换成院内集成平台查询；保持"查不到就 found=false"的语义不变即可，
    上层（模型/提示词/校验）完全不用改。
    """
    path = DATA_DIR / "patients.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def mask_name(name: str) -> str:
    if not name:
        return ""
    return name[0] + "*" * max(1, len(name) - 1)


def mask_ref(ref: str) -> str:
    if len(ref) <= 4:
        return "*" * len(ref)
    return ref[:2] + "*" * (len(ref) - 4) + ref[-2:]


def redact_arg(value: str) -> str:
    """审计表用：把可能的住院号/姓名打码，长度保留以便比对。"""
    if not value:
        return ""
    if len(value) <= 2:
        return "*" * len(value)
    return value[:1] + "*" * (len(value) - 2) + value[-1:]


async def resolve_patient(state: RunState, args: dict, db: AsyncSession) -> dict:
    ref = (args.get("patient_ref") or "").strip()
    if not ref:
        return {"found": False, "reason": "没有提供患者标识", "hint": "请向医护询问住院号或门诊号"}

    rows = _patients()
    hit = [p for p in rows if p["ref"].upper() == ref.upper()]
    if not hit:  # 退一步按姓名精确匹配，仍然要求唯一
        hit = [p for p in rows if p["name"] == ref]
    if len(hit) != 1:
        return {
            "found": False,
            "reason": "未找到唯一匹配的患者" if not hit else "匹配到多位同名患者",
            "candidates": [mask_name(p["name"]) for p in hit][:5],
            "hint": "请向医护核对住院号，不要猜测",
        }

    p = hit[0]
    state.patient_id = p["ref"]
    state.patient_label = f"{mask_name(p['name'])}（{p['dept']}）"
    state.plan_type = p.get("plan_type", "")
    state.discharge_date = p.get("discharge_date", "")
    today = date.today()
    try:
        base = datetime.strptime(p["discharge_date"], "%Y-%m-%d").date()
        state.flags.add("has_base_date")
        days = (today - base).days
    except Exception:
        base, days = None, None

    return {
        "found": True,
        "patient_id": p["ref"],  # 返回原文给服务端用；展示给模型的是掩码版
        "patient_display": f"{mask_name(p['name'])}（{p['dept']}）",
        "dept": p["dept"],
        "plan_type": p.get("plan_type", ""),
        "discharge_date": p.get("discharge_date", ""),
        "post_op_days": days,
        "note": "住院号为院内标识，回答中只可使用掩码形式",
    }


def _register_hits(state: RunState, hits: list) -> list[dict]:
    """把检索结果登记进本次运行，并给出跨多次检索连续的编号。

    为什么必须连续：模型查两轮后如果有两个 [1]，引用会串到错误的条文上。
    编号是"这次回答里第几条依据"，必须在整个 run 内唯一。
    """
    base = len(state.citations)
    items: list[dict] = []
    for i, h in enumerate(hits, 1):
        n = base + i
        items.append(
            {
                "n": n,
                "doc_title": h.doc_title,
                "doc_type": h.doc_type,
                "chunk_index": h.chunk_index,
                "content": h.content[:600],
            }
        )
        state.citations.append(
            {
                "n": n,
                "chunk_id": h.chunk_id,
                "document_id": h.document_id,
                "doc_title": h.doc_title,
                "doc_type": h.doc_type,
                "chunk_index": h.chunk_index,
                "preview": h.preview,
            }
        )
    return items


async def search_policy(state: RunState, args: dict, db: AsyncSession) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"hits": [], "reason": "检索词为空"}

    hits = await retrieve(db, query, top_k=3)
    if not hits:
        return {
            "hits": [],
            "reason": "知识库中没有已发布的制度能支撑该问题",
            "hint": "只能回复『随访规范中未查询到相关内容』，不要用通用医学知识补全",
        }

    items = _register_hits(state, hits)
    state.flags.add("searched")
    return {"hits": items, "count": len(items)}


async def list_existing_tasks(state: RunState, args: dict, db: AsyncSession) -> dict:
    patient_id = (args.get("patient_id") or state.patient_id or "").strip()
    if not patient_id:
        return {"items": [], "reason": "缺少 patient_id，请先调用 resolve_patient"}
    if state.patient_id and patient_id != state.patient_id:
        return {"items": [], "reason": "patient_id 与本轮已解析的患者不一致，请重新解析"}

    rows = (
        await db.execute(
            select(FollowupTaskDraft)
            .where(
                FollowupTaskDraft.patient_ref == patient_id,
                FollowupTaskDraft.status != "cancelled",
            )
            .order_by(FollowupTaskDraft.id.desc())
            .limit(5)
        )
    ).scalars().all()

    items = []
    for r in rows:
        for it in (r.content or {}).get("items", []):
            items.append(
                {
                    "offset_days": it.get("offset_days"),
                    "name": it.get("name", ""),
                    "status": r.status,
                }
            )
    return {"items": items[:20], "count": len(items), "drafts": len(rows)}


async def handoff_to_human(state: RunState, args: dict, db: AsyncSession) -> dict:
    reason = (args.get("reason") or "").strip() or "未说明原因"
    state.flags.add("handoff")
    return {"handoff": True, "reason": reason, "message": "已标记转人工，回答中请说明已转交并停止推断"}


async def check_followup_rule(state: RunState, args: dict, db: AsyncSession) -> dict:
    """用制度校验一组随访节点。passed 由规则引擎算，模型无权自判。

    三步：
      1. 取制度证据（检索）——没有证据就不给 pass，见 rules.py 头部第 5 条；
      2. 跑确定性规则（必检节点 / 区间 / 渠道 / 重复）；
      3. 通过才签发 rule_ticket（HMAC，绑定患者 + 规则指纹 + 5 分钟有效期）。
    """
    patient_id = (args.get("patient_id") or state.patient_id or "").strip()
    if not patient_id:
        return {"ok": False, "error": "patient_required", "hint": "请先调用 resolve_patient 确认患者"}
    if state.patient_id and patient_id != state.patient_id:
        return {
            "ok": False,
            "error": "patient_mismatch",
            "hint": "patient_id 与本轮已解析的患者不一致，请重新解析后再校验",
        }

    plan_type = (args.get("plan_type") or state.plan_type or "常规术后随访").strip()[:64]

    # 1. 取证据：检索词带上方案名，尽量命中该病种的随访节点条款
    hits = await retrieve(db, f"{plan_type} 随访 时间 节点 要求", top_k=2)
    evidence = _register_hits(state, hits) if hits else []
    evidence_ids = [c["chunk_id"] for c in state.citations[-len(evidence) :]] if evidence else []

    # 2. 确定性判定
    verdict = rules.evaluate(plan_type, args.get("items"), evidence_ids=evidence_ids)
    payload = verdict.as_dict()
    payload["evidence"] = [
        {"n": e["n"], "doc_title": e["doc_title"], "content": e["content"][:160]}
        for e in evidence
    ]
    payload["ok"] = True

    # 3. 通过才发票
    if verdict.passed:
        sha = rules.rule_sha(verdict, evidence_ids)
        ticket = issue_ticket(
            kind="rule",
            run_uid=state.run_uid,
            patient_id=state.patient_id,
            plan_type=plan_type,
            rule_sha=sha,
        )
        state.rule_ticket = ticket
        state.rule_sha = sha
        state.plan_type = plan_type
        state.flags.add("rule_passed")
        payload["rule_ticket"] = ticket
        payload["rule_sha"] = sha
        payload["note"] = (
            "校验通过。rule_ticket 是创建草稿的唯一凭证，5 分钟内有效、只能用一次；"
            "创建草稿前不要再自行判断合规性。"
        )
    else:
        state.flags.add("rule_failed")
        payload["hint"] = "不得为了让校验通过而自行改动医护的方案；请说明违反了哪条，等医护确认后再重新校验。"

    return payload


async def assess_red_flag(state: RunState, args: dict, db: AsyncSession) -> dict:
    """红旗症状评估：纯关键词表 + 制度处置动作，不做"像不像"的语义猜测。

    宁可多报一条让医护复核，也不要把"胸闷"判成"没事"——
    漏报的代价是患者安全，误报的代价只是多一通电话。
    """
    symptoms = (args.get("symptoms") or "").strip()
    if not symptoms:
        return {"ok": False, "error": "symptoms_required", "hint": "请把医护描述的症状原文传给我"}

    hits = rules.match_red_flags(symptoms)
    level = rules.risk_level(hits)

    # 处置动作同样要有制度出处，不能只给"建议就医"这种通用话
    policy_hits = await retrieve(db, "红旗症状 处置 升级 发热 胸痛", top_k=2)
    evidence = _register_hits(state, policy_hits) if policy_hits else []

    payload: dict = {
        "ok": True,
        "level": level,
        "hits": hits,
        "evidence": [
            {"n": e["n"], "doc_title": e["doc_title"], "content": e["content"][:200]}
            for e in evidence
        ],
    }
    # 患者信息只用于归属升级工单；没解析患者也要能出结论（抢救优先）
    if state.patient_id:
        payload["patient_id"] = state.patient_id

    state.flags.add(f"red_flag_{level}")
    if level == "high":
        ticket = issue_ticket(
            kind="risk",
            run_uid=state.run_uid,
            patient_id=state.patient_id,
            plan_type=state.plan_type,
        )
        state.risk_ticket = ticket
        payload["risk_ticket"] = ticket
        payload["mandatory"] = (
            "命中高危红旗症状：回答必须先给出处置动作（联系主管医生/急救），"
            "并调用 escalate_to_clinician 建升级工单；不得只回复「继续观察」。"
        )
    elif level == "medium":
        payload["mandatory"] = "中风险：提示当日电话复核，并在回答中说明依据条文。"
    else:
        payload["note"] = "未命中红旗症状关键词。若医护补充了新症状，必须重新评估。"
    return payload


async def create_followup_task_draft(state: RunState, args: dict, db: AsyncSession) -> dict:
    """★ 写操作闸门：这里**不写库**。

    它做三件事：验票（一次性）→ 归一化草稿内容 → 返回一张"待确认单"。
    真正的落库发生在医护点击确认之后（`/agent/runs/{uid}/confirm`）。

    为什么不在工具里直接建 draft 再让人确认：
      draft 一旦进了库，就会出现在任务列表、可能被其他流程读到；
      "确认前不存在"比"确认前存在但标着待确认"干净得多。
    """
    ticket = (args.get("rule_ticket") or state.rule_ticket or "").strip()
    if not ticket:
        return {
            "ok": False,
            "error": "rule_ticket_required",
            "hint": "没有 rule_ticket，说明还没有通过 check_followup_rule。请先校验。",
        }

    try:
        # 先纯验签（不消费）：参数不合法时不要把票烧掉，
        # 否则医护补一句信息就得重新走一遍校验，体验和成本都不合理。
        verify_ticket(ticket, kind="rule", patient_id=state.patient_id)
    except AppError as exc:
        return {"ok": False, "error": exc.code, "hint": exc.message}

    try:
        normalized = task_service.normalize(
            {
                "patient_ref": args.get("patient_ref") or state.patient_id,
                "plan_type": args.get("plan_type") or state.plan_type,
                "items": args.get("items"),
                "rationale": args.get("rationale"),
            },
            base_date=_base_date(state),
            patient_hint=state.patient_id,
        )
    except task_service.TaskInputError as exc:
        # 信息不足要让模型去问医护，而不是自己补一个"看起来合理"的值
        return {"ok": False, "error": "input_incomplete", "hint": str(exc)}

    try:
        # 参数齐了再一次性消费：同一张票第二次建草稿会被 ticket_nonces 挡下
        payload = await consume_ticket(db, ticket, kind="rule", patient_id=state.patient_id)
    except AppError as exc:
        return {"ok": False, "error": exc.code, "hint": exc.message}

    preview = _draft_preview(normalized)
    state.flags.add("draft_pending")
    return {
        "ok": True,
        "pending_confirm": True,
        "message": "草稿已生成，等待医护确认后才会写入系统；确认前请勿声称已创建或已安排。",
        "draft": preview,
        "rule_sha": str(payload.get("s") or ""),
        # 归一化后的完整参数只给服务端建确认单用，回灌给模型时会被截断
        "normalized": _jsonable(normalized),
    }


async def escalate_to_clinician(state: RunState, args: dict, db: AsyncSession) -> dict:
    """升级主管医生：必须持 risk_ticket 或 rule_ticket（同样一次性消费）。"""
    token = (args.get("ticket") or state.risk_ticket or state.rule_ticket or "").strip()
    if not token:
        return {
            "ok": False,
            "error": "ticket_required",
            "hint": "升级必须先完成红旗评估或制度校验，拿到对应票据",
        }

    used_kind = ""
    for kind in ("risk", "rule"):
        try:
            await consume_ticket(db, token, kind=kind, patient_id=state.patient_id or None)
            used_kind = kind
            break
        except AppError:
            continue
    if not used_kind:
        return {"ok": False, "error": "AGENT_TICKET_INVALID", "hint": "票据无效、过期或已使用"}

    reason = (args.get("reason") or "").strip()[:200] or "未说明原因"
    state.flags.add("escalated")
    state.flags.add(f"escalate_by_{used_kind}")
    escalation_no = f"ESC-{state.run_uid[:8].upper()}"
    return {
        "ok": True,
        "escalated": True,
        "escalation_no": escalation_no,
        "ticket_kind": used_kind,
        "reason": reason,
        # 演示版：真实部署在这里调 HIS/企业微信通知；表结构见 README「已知边界」
        "notify": "已生成升级工单，待接入院内通知通道（演示环境仅记录）",
        "hint": "回答中要说明已升级并写明接手人待确认，不得声称医生已处理",
    }


# ---------------------------------------------------------------- 内部工具


def _base_date(state: RunState) -> date:
    """出院日 = 第 0 天。解析不到就用今天，并在 flags 里留痕。"""
    try:
        return datetime.strptime(state.discharge_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        state.flags.add("base_date_today_fallback")
        return date.today()


def _jsonable(payload: dict) -> dict:
    """datetime → ISO，方便塞进 JSON 列。"""
    out = {}
    for k, v in payload.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _draft_preview(normalized: dict) -> dict:
    """给前端确认卡片看的预览：患者标识掩码、节点带日期，不含内部字段。"""
    return {
        "patient_ref": mask_ref(normalized["patient_ref"]),
        "plan_type": normalized["plan_type"],
        "scheduled_at": _jsonable(normalized)["scheduled_at"],
        "rationale": normalized["rationale"],
        "items": normalized["items"],
        "flags": normalized["flags"],
    }
