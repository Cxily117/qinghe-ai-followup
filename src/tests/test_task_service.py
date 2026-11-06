"""随访任务状态机：draft → confirmed / cancelled，以及「AI 不许自己确认」这条硬约束。

测的是状态流转与落库结果，不是文案。以下几条一旦被改动，必须红：
  1. 客户端/模型无法让草稿直接变成 confirmed —— 只能经 `confirm()` 这个带 user_id 的入口；
  2. confirmed 之后不能再取消 —— 已生效的随访计划要走撤销流程，不是「取消草稿」；
  3. 确认时可以改节点，但日期必须按同一出院日重算，不能沿用旧 due_date；
  4. 模型编造的住院号必须被拒，宁可 422 也不写假数据进库；
  5. 相同患者+方案+节点在 24 小时内重复点击，复用原草稿而不是堆垃圾数据。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.agents.gateway.base import ChatResult, ToolCall
from app.models.task import FollowupTaskDraft
from app.schemas.task import TaskConfirmIn, TaskItemIn
from app.services import task_service

BASE = date(2026, 11, 1)

# mock 供应商能识别的两句话：一句带住院号与两个节点，一句故意不带住院号
Q_OK = "患者住院号 ZY2026001，骨科术后，第 7 天电话随访，第 30 天门诊复查"
Q_NO_PATIENT = "新建一份随访，第 7 天电话随访"


class StubLLM:
    """把一个「会说谎的模型」塞进链路：它给出的住院号在对话里根本没出现过。"""

    def __init__(self, arguments: dict) -> None:
        self.arguments = arguments

    async def chat_tools(self, *args, **kwargs) -> ChatResult:
        call = ToolCall(name="create_followup_task_draft", arguments=self.arguments)
        return ChatResult(text="", model="stub", extra={"tool_calls": [call]})


# ---------------------------------------------------------------- 入参归一化
def test_normalize_computes_due_dates_and_first_schedule() -> None:
    out = task_service.normalize(
        {
            "patient_ref": "ZY2026001",
            "plan_type": "骨科术后随访",
            "items": [
                {"name": "电话随访", "offset_days": 7, "channel": "phone"},
                {"name": "门诊复查", "offset_days": 30, "channel": "outpatient"},
            ],
        },
        base_date=BASE,
        patient_hint=None,
    )
    assert out["patient_ref"] == "ZY2026001"
    # 日期必须由基准日 + offset 算，不能信模型给的日期
    assert [i["due_date"] for i in out["items"]] == ["2026-11-08", "2026-12-01"]
    # 首个节点日 09:00 排程
    assert out["scheduled_at"].date() == BASE + timedelta(days=7)


def test_normalize_merges_same_day_and_drops_illegal_offsets() -> None:
    out = task_service.normalize(
        {
            "patient_ref": "P1",
            "items": [
                {"name": "抽血", "offset_days": 7},
                {"name": "电话随访", "offset_days": 7},
                {"name": "越界", "offset_days": 999},
                {"name": "非数字", "offset_days": "第7天"},
            ],
        },
        base_date=BASE,
        patient_hint=None,
    )
    assert len(out["items"]) == 1  # 同一天合并、非法 offset 丢弃
    assert "抽血" in out["items"][0]["name"] and "电话随访" in out["items"][0]["name"]
    assert out["items"][0]["due_date"] == "2026-11-08"


def test_normalize_rejects_missing_patient_and_empty_items() -> None:
    with pytest.raises(task_service.TaskInputError, match="患者标识"):
        task_service.normalize({"items": [{"offset_days": 7}]}, base_date=BASE, patient_hint=None)
    with pytest.raises(task_service.TaskInputError, match="随访时间点"):
        task_service.normalize(
            {"patient_ref": "P1", "items": []}, base_date=BASE, patient_hint=None
        )


def test_patient_hint_fills_gap_and_flags_it() -> None:
    out = task_service.normalize(
        {"items": [{"offset_days": 3}]}, base_date=BASE, patient_hint="ZY2026009"
    )
    assert out["patient_ref"] == "ZY2026009"
    # 来源要留痕：这一条是「人补的」还是「模型抽的」，复盘时不一样
    assert out["flags"]["patient_ref_from_hint"] is True


# ---------------------------------------------------------------- 生成
@pytest.mark.asyncio
async def test_generate_draft_stops_at_draft(db, staff) -> None:
    draft, reused = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )
    assert reused is False
    assert draft.status == "draft"  # AI 永远只能到 draft
    assert draft.confirmed_by is None  # 没有人签过字
    assert draft.patient_ref == "ZY2026001"
    assert [i["offset_days"] for i in draft.content["items"]] == [7, 30]
    assert draft.scheduled_at.date() == BASE + timedelta(days=7)


@pytest.mark.asyncio
async def test_generate_draft_rejects_missing_patient_id(db, staff) -> None:
    # mock 在找不到标识时返回空串，服务端必须 422 而不是造一个
    with pytest.raises(task_service.TaskInputError, match="患者标识"):
        await task_service.generate_draft(
            db, user_id=staff.id, question=Q_NO_PATIENT, base_date=BASE
        )


@pytest.mark.asyncio
async def test_hallucinated_patient_id_is_rejected(db, staff, monkeypatch) -> None:
    """模型编了个住院号（对话里没有），必须拒绝——这条靠模型自觉是保不住的。"""
    fake = StubLLM(
        {
            "patient_ref": "P123456",
            "plan_type": "骨科术后随访",
            "items": [{"name": "电话随访", "offset_days": 7}],
        }
    )
    monkeypatch.setattr(task_service, "get_llm", lambda: fake)
    with pytest.raises(task_service.TaskInputError, match="没有出现过"):
        await task_service.generate_draft(
            db, user_id=staff.id, question="给这个病人排一下随访，第 7 天电话", base_date=BASE
        )
    # 库里不能留下任何草稿
    assert (await _all_drafts(db)) == []


@pytest.mark.asyncio
async def test_generate_draft_dedupes_within_24h(db, staff) -> None:
    first, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )
    second, reused = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )
    assert reused is True
    assert second.id == first.id  # 复用同一条，不堆重复草稿
    assert second.content["ai"]["reused"] is True
    assert len(await _all_drafts(db)) == 1


# ---------------------------------------------------------------- 人工确认
@pytest.mark.asyncio
async def test_confirm_turns_draft_into_confirmed_with_signature(db, staff) -> None:
    draft, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )

    confirmed = await task_service.confirm(
        db, draft, user_id=staff.id, username="nurse01", payload=TaskConfirmIn(note="已电话核对")
    )

    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_by == staff.id
    sign = confirmed.content["confirm"]
    assert sign["by"] == "nurse01" and sign["by_id"] == staff.id
    assert sign["note"] == "已电话核对"
    # 记下 AI 原给的节点数，用于评估抽取质量（人工改了几个）
    assert sign["ai_item_count"] == 2


@pytest.mark.asyncio
async def test_confirm_recomputes_due_dates_from_same_base_date(db, staff) -> None:
    """确认时改节点：日期必须按原出院日重算，不能把旧 due_date 带过去。"""
    draft, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )

    confirmed = await task_service.confirm(
        db,
        draft,
        user_id=staff.id,
        username="nurse01",
        payload=TaskConfirmIn(
            plan_type="骨科术后随访（修订）",
            items=[
                TaskItemIn(name="出院前评估", offset_days=0),
                TaskItemIn(name="电话随访", offset_days=14),
            ],
        ),
    )

    items = confirmed.content["items"]
    assert [i["offset_days"] for i in items] == [0, 14]
    assert [i["due_date"] for i in items] == ["2026-11-01", "2026-11-15"]
    assert confirmed.plan_type == "骨科术后随访（修订）"
    # 排程跟着首个节点走（第 0 天 09:00）
    assert confirmed.scheduled_at.date() == BASE


@pytest.mark.asyncio
async def test_confirm_is_idempotent_guard_not_upgradeable(db, staff) -> None:
    draft, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )
    payload = TaskConfirmIn()
    await task_service.confirm(db, draft, user_id=staff.id, username="nurse01", payload=payload)

    with pytest.raises(task_service.TaskInputError, match="只有 draft 状态可确认"):
        await task_service.confirm(db, draft, user_id=staff.id, username="nurse01", payload=payload)


# ---------------------------------------------------------------- 取消
@pytest.mark.asyncio
async def test_cancel_records_who_and_when(db, staff) -> None:
    draft, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )
    cancelled = await task_service.cancel(db, draft, user_id=staff.id, username="nurse01")

    assert cancelled.status == "cancelled"
    assert cancelled.content["cancel"]["by"] == "nurse01"
    assert cancelled.confirmed_by is None


@pytest.mark.asyncio
async def test_confirmed_task_cannot_be_cancelled(db, staff) -> None:
    """已生效的随访计划不能「取消草稿」，必须走撤销流程（D6）——否则现场会有人拿这个当撤销用。"""
    draft, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )
    await task_service.confirm(
        db, draft, user_id=staff.id, username="nurse01", payload=TaskConfirmIn()
    )

    with pytest.raises(task_service.TaskInputError, match="不能取消"):
        await task_service.cancel(db, draft, user_id=staff.id, username="nurse01")


# ---------------------------------------------------------------- 越权
@pytest.mark.asyncio
async def test_draft_is_only_visible_to_owner_or_admin(db, staff, other_staff, admin) -> None:
    draft, _ = await task_service.generate_draft(
        db, user_id=staff.id, question=Q_OK, base_date=BASE
    )

    assert (
        await task_service.get_draft_for_user(db, draft.id, user_id=staff.id, is_admin=False)
        is not None
    )
    # 同事看不到（草稿里有患者标识，不能横向可见）
    assert (
        await task_service.get_draft_for_user(db, draft.id, user_id=other_staff.id, is_admin=False)
        is None
    )
    # 管理员代查得到
    assert (
        await task_service.get_draft_for_user(db, draft.id, user_id=admin.id, is_admin=True)
        is not None
    )
    assert await task_service.get_draft_for_user(db, 9999, user_id=staff.id, is_admin=True) is None


async def _all_drafts(db) -> list[FollowupTaskDraft]:
    from sqlalchemy import select

    return list((await db.execute(select(FollowupTaskDraft))).scalars().all())
