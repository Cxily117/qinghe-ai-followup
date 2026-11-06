"""患者目录的读写（唯一入口）。

三个使用方：
  - 智能体工具 `resolve_patient`：按住院号/姓名精确查，查不到就 found=false，绝不猜；
  - 管理后台 `/admin/patients`：列表、搜索、新增；
  - 启动播种：把仓库自带的演示患者导进库（幂等，按 patient_ref 去重）。

为什么查不到要返回"候选"而不是"最像的那一个"：
  同名患者张冠李戴，后面所有随访节点都会挂到错误的人身上。
  宁可让医护多打一次住院号，也不能让模型"猜一个"。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.patient import Patient

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent.parent / "agents" / "data" / "patients.json"

MAX_PAGE = 200


class PatientInputError(ValueError):
    """录入参数不合法：消息直接显示给管理员，不要写内部细节。"""


# ------------------------------------------------------------------ 播种


async def ensure_seed(db: AsyncSession) -> int:
    """把 patients.json 里的演示患者补齐进库，返回新增条数。

    幂等：已存在同 patient_ref 的跳过。真实部署把这一步换成 HIS 同步即可，
    表结构和上层代码都不用改。
    """
    if not SEED_FILE.exists():
        return 0
    try:
        rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("患者种子文件读取失败：%s", SEED_FILE, exc_info=True)
        return 0

    existing = set((await db.execute(select(Patient.patient_ref))).scalars().all())
    added = 0
    for row in rows:
        ref = (row.get("ref") or "").strip()
        if not ref or ref in existing:
            continue
        db.add(
            Patient(
                patient_ref=ref,
                name=(row.get("name") or "").strip()[:64],
                dept=(row.get("dept") or "").strip()[:64],
                plan_type=(row.get("plan_type") or "").strip()[:64],
                discharge_date=_parse_date(row.get("discharge_date")),
                status="active",
                source="seed",
                note=(row.get("note") or "演示数据")[:200],
            )
        )
        added += 1
    if added:
        await db.commit()
        logger.info("患者目录播种完成：新增 %d 条", added)
    return added


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ------------------------------------------------------------------ 查询


async def find_exact(db: AsyncSession, raw: str) -> tuple[Patient | None, list[Patient]]:
    """先按住院号，再按姓名；必须唯一命中，否则返回候选让调用方去问。

    返回 (命中的患者 or None, 候选列表)。
    """
    key = (raw or "").strip()
    if not key:
        return None, []

    rows = (
        (
            await db.execute(
                select(Patient).where(
                    func.upper(Patient.patient_ref) == key.upper(),
                    Patient.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        rows = (
            (
                await db.execute(
                    select(Patient).where(Patient.name == key, Patient.status == "active")
                )
            )
            .scalars()
            .all()
        )
    if len(rows) == 1:
        return rows[0], []
    return None, list(rows)


async def list_page(
    db: AsyncSession, *, q: str | None = None, page: int = 1, page_size: int = 20
) -> dict:
    stmt = select(Patient)
    count_stmt = select(func.count()).select_from(Patient)
    if q:
        like = f"%{q.strip()}%"
        cond = or_(
            Patient.patient_ref.ilike(like), Patient.name.ilike(like), Patient.dept.ilike(like)
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await db.execute(count_stmt)).scalar_one()
    page = max(1, page)
    page_size = min(max(1, page_size), MAX_PAGE)
    items = (
        (
            await db.execute(
                stmt.order_by(Patient.id.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": list(items),
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


async def stats(db: AsyncSession) -> dict:
    total = (await db.execute(select(func.count()).select_from(Patient))).scalar_one()
    seeded = (
        await db.execute(select(func.count()).select_from(Patient).where(Patient.source == "seed"))
    ).scalar_one()
    return {"total": total, "seeded": seeded, "manual": total - seeded}


async def get_by_ref(db: AsyncSession, ref: str) -> Patient | None:
    return (
        await db.execute(select(Patient).where(Patient.patient_ref == ref))
    ).scalar_one_or_none()


# ------------------------------------------------------------------ 写入


async def create(db: AsyncSession, payload: dict) -> Patient:
    ref = (payload.get("patient_ref") or "").strip()
    name = (payload.get("name") or "").strip()
    if not ref:
        raise PatientInputError("住院号不能为空")
    if len(ref) > 64:
        raise PatientInputError("住院号过长（最多 64 字符）")
    if not name:
        raise PatientInputError("患者姓名不能为空")

    if await get_by_ref(db, ref) is not None:
        raise PatientInputError(f"住院号 {ref} 已存在，请核对后再录入")

    patient = Patient(
        patient_ref=ref,
        name=name[:64],
        dept=(payload.get("dept") or "").strip()[:64],
        plan_type=(payload.get("plan_type") or "").strip()[:64],
        discharge_date=_parse_date(payload.get("discharge_date")),
        phone=(payload.get("phone") or "").strip()[:32] or None,
        status="active",
        source="manual",
        note="管理员手工录入"[:200],
    )
    db.add(patient)
    await db.commit()
    await db.refresh(patient)
    return patient


async def set_status(db: AsyncSession, patient: Patient, status: str) -> Patient:
    if status not in ("active", "inactive"):
        raise PatientInputError("状态只能是 active / inactive")
    patient.status = status
    await db.commit()
    await db.refresh(patient)
    return patient
