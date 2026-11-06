"""审计落库与脱敏：库里到底存了什么，是这个模块唯一要证明的事。

两条线：
  1. 脱敏线 —— 审计表是「多落一份 PHI」的高危位置。入参、问题原文、工具结果
     在写库**之前**就要打码，所以断言方式是不看函数返回值，而是回读数据库，
     确认整行 JSON 里查不到住院号/手机号/身份证原文。
  2. 标记线 —— 人工定性要能追溯：同一会话同级不重复、撤销是软撤回（留痕），
     钉错会话必须拒绝（否则审计串线就是无效证据）。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app.agents.harness import RoundRecord, redact_args
from app.agents.runtime import RunState
from app.core.errors import AppError
from app.models.agent import AgentRun, AgentToolCall
from app.models.audit import ChatAuditMark
from app.services import agent_service, audit_service
from tests.conftest import add_session

# 一段同时含住院号、手机号、身份证的「医护原话」——最容易被随手写进日志的形态
RAW_QUESTION = (
    "患者住院号 ZY2026001 术后第 7 天发热 38.9，联系电话 13812345678，身份证 110101199001011234"
)
PHI = ("ZY2026001", "13812345678", "110101199001011234")


# ---------------------------------------------------------------- 脱敏：落库后不可反查
@pytest.mark.asyncio
async def test_tool_call_args_are_masked_before_hitting_db(db, staff) -> None:
    session = await add_session(db, user_id=staff.id)
    run = AgentRun(run_uid="run-mask-1", session_id=session.id, user_id=staff.id)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    record = RoundRecord(
        round_no=1,
        decision="tool_call",
        tool="resolve_patient",
        args_redacted=redact_args(
            {
                "patient_ref": "ZY2026001",
                "query": "住院号 ZY2026001 术后发热",
                "symptoms": "伤口渗液，体温 38.9",
                "plan_type": "骨科术后随访",
                "days": 7,
            }
        ),
        result_digest="ok",
    )
    await agent_service._persist_round(
        db, run.id, record, RunState(run_uid="run-mask-1", user_id=staff.id)
    )

    # 回读数据库：不是看内存对象，而是看真正落盘的那一行
    row = (
        (await db.execute(select(AgentToolCall).where(AgentToolCall.run_id == run.id)))
        .scalars()
        .one()
    )
    dumped = json.dumps(row.args_redacted, ensure_ascii=False)

    assert "ZY2026001" not in dumped  # 住院号打码
    assert "伤口渗液" not in dumped  # 症状原文按 key 打码
    assert row.args_redacted["plan_type"] == "骨科术后随访"  # 非 PHI 保留，否则没法复盘
    assert row.args_redacted["days"] == 7  # 非字符串原样保留
    assert row.tool == "resolve_patient"
    assert row.seq == 1


@pytest.mark.asyncio
async def test_run_question_strips_phone_and_idcard(db, staff) -> None:
    session = await add_session(db, user_id=staff.id)
    run = AgentRun(
        run_uid="run-mask-2",
        session_id=session.id,
        user_id=staff.id,
        question=agent_service.redact_question(RAW_QUESTION),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    stored = (
        (await db.execute(select(AgentRun).where(AgentRun.id == run.id))).scalars().one().question
    )
    for raw in PHI:
        assert raw not in stored
    # 仍要能人工比对：住院长号保留前两位、手机号保留首三位
    assert "ZY" in stored and "138" in stored


@pytest.mark.asyncio
async def test_business_message_content_is_kept_intact(db, staff) -> None:
    """对比项：messages.content 是医护自己回看的业务记录，**不该**被打码。

    这条防的是「矫枉过正」——有人为了合规把业务正文也脱敏，随访记录就没法用了。
    """
    session = await add_session(
        db,
        user_id=staff.id,
        messages=[{"role": "user", "content": RAW_QUESTION}],
    )
    detail = await audit_service.session_detail(db, session.id)
    assert detail["messages"][0]["content"] == RAW_QUESTION


# ---------------------------------------------------------------- 人工标记
@pytest.mark.asyncio
async def test_add_mark_and_dedupe_same_level(db, staff, admin) -> None:
    session = await add_session(db, user_id=staff.id)
    first = await audit_service.add_mark(
        db, session_id=session.id, admin_id=admin.id, level="high_risk", note="应建议就诊"
    )
    again = await audit_service.add_mark(
        db, session_id=session.id, admin_id=admin.id, level="high_risk", note="重复点击"
    )
    assert first.id == again.id  # 重复点击返回已有记录，不产生两条"高危"
    assert first.note == "应建议就诊"

    # 不同级别是另一件事，允许并存
    watch = await audit_service.add_mark(
        db, session_id=session.id, admin_id=admin.id, level="watch"
    )
    assert watch.id != first.id


@pytest.mark.asyncio
async def test_mark_rejects_bad_level_and_wrong_session(db, staff, other_staff, admin) -> None:
    session = await add_session(db, user_id=staff.id)
    other = await add_session(
        db, user_id=other_staff.id, messages=[{"role": "user", "content": "别处的问题"}]
    )
    foreign_msg_id = (await db.execute(select(ChatAuditMark))).scalars().all()  # 空表占位
    assert foreign_msg_id == []

    with pytest.raises(AppError) as exc:
        await audit_service.add_mark(db, session_id=session.id, admin_id=admin.id, level="critical")
    assert exc.value.code == "VALIDATION_ERROR"

    with pytest.raises(AppError) as exc:
        await audit_service.add_mark(db, session_id=999_999, admin_id=admin.id)
    assert exc.value.code == "SESSION_NOT_FOUND"

    # 把 B 会话的消息钉到 A 会话上：审计会串线，必须拒绝
    from app.models.chat import Message

    msg = (
        (await db.execute(select(Message).where(Message.session_id == other.id))).scalars().first()
    )
    with pytest.raises(AppError) as exc:
        await audit_service.add_mark(
            db, session_id=session.id, admin_id=admin.id, message_id=msg.id
        )
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_revoke_is_soft_delete_and_idempotent(db, staff, admin) -> None:
    session = await add_session(db, user_id=staff.id)
    mark = await audit_service.add_mark(db, session_id=session.id, admin_id=admin.id)

    revoked = await audit_service.revoke_mark(db, mark_id=mark.id, admin_id=admin.id)
    assert revoked.revoked_at is not None
    assert revoked.revoked_by == admin.id

    # 记录没被物理删：审计痕迹不能悄悄消失
    still_there = (
        (await db.execute(select(ChatAuditMark).where(ChatAuditMark.id == mark.id))).scalars().one()
    )
    assert still_there.revoked_at is not None

    again = await audit_service.revoke_mark(db, mark_id=mark.id, admin_id=admin.id)
    assert again.revoked_at == revoked.revoked_at  # 幂等

    with pytest.raises(AppError) as exc:
        await audit_service.revoke_mark(db, mark_id=999_999, admin_id=admin.id)
    assert exc.value.code == "MARK_NOT_FOUND"


# ---------------------------------------------------------------- 审计列表
@pytest.mark.asyncio
async def test_query_sessions_only_covers_staff_and_flags_risk(
    db, staff, other_staff, admin
) -> None:
    risky = await add_session(
        db,
        user_id=staff.id,
        title="术后发热咨询",
        messages=[
            {"role": "user", "content": "患者发烧 39 度怎么办"},
            {
                "role": "assistant",
                "content": "请立即联系主管医生",
                "meta": {"flags": {"red_flag_question": True, "uncited_answer": True}},
            },
        ],
    )
    calm = await add_session(
        db,
        user_id=other_staff.id,
        title="复查时间",
        messages=[
            {"role": "user", "content": "第 30 天复查要空腹吗"},
            {"role": "assistant", "content": "不需要", "meta": {"flags": {"refusal": True}}},
        ],
    )
    # 管理员自己的会话不进审计列表（审的是医护问答）
    await add_session(db, user_id=admin.id, title="管理员自测")

    page = await audit_service.query_sessions(db)
    ids = [i["session_id"] for i in page["items"]]
    assert set(ids) == {risky.id, calm.id}
    assert page["total"] == 2

    flagged = await audit_service.query_sessions(db, only_flagged=True)
    assert [i["session_id"] for i in flagged["items"]] == [risky.id]
    # refusal 不算风险项：拒答是正确行为，不该进待复核队列
    assert flagged["items"][0]["risk_count"] == 2
    assert flagged["items"][0]["suspected"] is True
    assert flagged["summary"]["suspected"] == 1


@pytest.mark.asyncio
async def test_query_sessions_filters_marks_and_pages(db, staff, admin) -> None:
    sessions = []
    for idx in range(3):
        sessions.append(
            await add_session(
                db,
                user_id=staff.id,
                title=f"会话{idx}",
                messages=[
                    {"role": "user", "content": "问题"},
                    {"role": "assistant", "content": "回答", "meta": {"flags": {}}},
                ],
            )
        )
    await audit_service.add_mark(db, session_id=sessions[0].id, admin_id=admin.id)

    marked = await audit_service.query_sessions(db, only_marked=True)
    assert [i["session_id"] for i in marked["items"]] == [sessions[0].id]
    assert marked["items"][0]["marks"][0]["level_label"] == "疑似高危"

    page1 = await audit_service.query_sessions(db, page=1, page_size=2)
    page2 = await audit_service.query_sessions(db, page=2, page_size=2)
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    assert page1["pages"] == 2
    assert {i["session_id"] for i in page1["items"]} | {
        i["session_id"] for i in page2["items"]
    } == {s.id for s in sessions}

    # 按人筛：只有该医护的会话
    by_user = await audit_service.query_sessions(db, user_id=staff.id)
    assert by_user["total"] == 3
    assert (await audit_service.query_sessions(db, user_id=999_999))["total"] == 0

    # 按日期筛：今天查得到，明天查不到
    today = await audit_service.query_sessions(db, date_from=date.today(), date_to=date.today())
    assert today["total"] == 3


@pytest.mark.asyncio
async def test_session_detail_exposes_citations_and_active_marks(db, staff, admin) -> None:
    session = await add_session(
        db,
        user_id=staff.id,
        messages=[
            {"role": "user", "content": "第 7 天要做什么"},
            {
                "role": "assistant",
                "content": "电话随访并询问切口情况",
                "meta": {
                    "flags": {"red_flag_question": True},
                    "citation_detail": [{"title": "骨科术后随访制度", "chunk_index": 2}],
                    "model": "glm-4-flash",
                    "prompt_version": "policy_qa@v3",
                    "latency_ms": 820,
                },
            },
        ],
    )
    mark = await audit_service.add_mark(db, session_id=session.id, admin_id=admin.id)

    detail = await audit_service.session_detail(db, session.id)
    msg = detail["messages"][1]
    assert msg["flag_labels"] == ["红旗问题"]
    assert msg["citations"][0]["chunk_index"] == 2
    assert msg["model"] == "glm-4-flash"
    assert msg["latency_ms"] == 820
    assert detail["suspected"] is True
    assert detail["marks"][0]["marked_by"] == "admin01"  # 显示人名，不暴露 id 之外的东西
    assert len(detail["active_marks"]) == 1

    # 会话不存在要说清楚，不能返回空结构让前端误以为"没有问题"
    with pytest.raises(AppError) as exc:
        await audit_service.session_detail(db, 999_999)
    assert exc.value.code == "SESSION_NOT_FOUND"

    # 撤销后 active_marks 归零，但 marks 里仍看得到这条历史
    await audit_service.revoke_mark(db, mark_id=mark.id, admin_id=admin.id)
    after = await audit_service.session_detail(db, session.id)
    assert after["active_marks"] == []
    assert after["marks"][0]["revoked_at"] is not None


@pytest.mark.asyncio
async def test_stats_and_staff_list(db, staff, other_staff, admin) -> None:
    session = await add_session(
        db,
        user_id=staff.id,
        messages=[
            {"role": "user", "content": "问"},
            {"role": "assistant", "content": "答"},
        ],
    )
    await audit_service.add_mark(db, session_id=session.id, admin_id=admin.id)

    stats = await audit_service.stats(db)
    assert stats["sessions_total"] == 1
    assert stats["answers_today"] == 1
    assert stats["marks_active"] == 1

    people = await audit_service.list_staff(db)
    assert {p.username for p in people} == {"nurse01", "nurse02"}  # 管理员不在"医护"名单里
