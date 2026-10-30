"""工具的服务端执行体（只读部分）。

原则：**工具返回的是「事实」，不是「结论」**。
  - `resolve_patient` 只返回院内的患者记录，查不到就 found=false，绝不替模型猜；
  - `search_policy` 只返回条文原文片段，不替模型判断"这算不算合规"；
  - `list_existing_tasks` 只返回已排节点，去重判断留给服务端在写操作时做。

这一层是"可举证"的落点：模型说的每句话要么来自这里的返回，要么就是它编的。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.rag import retrieve
from app.agents.runtime import RunState
from app.models.task import FollowupTaskDraft

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

    # 跨多次检索连续编号：第二轮检索的编号接着第一轮往后排，
    # 否则模型"查两次"后 [1] 会指到两条不同的条文，引用必然串。
    base = len(state.citations)
    items = []
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
