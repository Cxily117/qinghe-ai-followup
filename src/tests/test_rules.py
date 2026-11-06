"""规则引擎单测：验的是"能不能判出问题"，不是"文案好不好看"。

这些用例的价值在于：规则档位与违规码一旦被改动（比如有人把 30 天门诊改成可选），
测试会立刻失败，而不是等到现场演示时才发现排期校验放行了。
"""

from __future__ import annotations

from app.agents import rules

# 有制度证据才可能 pass——这是引擎的硬口径（NO_POLICY_EVIDENCE）
EVIDENCE = [7]


def node(offset: int, name: str = "", channel: str = "") -> dict:
    return {"offset_days": offset, "name": name or f"第{offset}天随访", "channel": channel}


def codes(verdict: rules.RuleVerdict) -> set[str]:
    return {v.code for v in verdict.violations}


def test_ortho_complete_plan_passes() -> None:
    verdict = rules.evaluate(
        "骨科关节置换术后",
        [node(3, "出院前评估"), node(7, "电话随访", "phone"), node(30, "门诊复查", "outpatient")],
        evidence_ids=EVIDENCE,
    )
    assert verdict.passed is True
    assert verdict.profile_key == "ortho_postop"
    assert verdict.profile_known is True
    assert verdict.normalized_offsets == [3, 7, 30]


def test_missing_required_node_is_blocking() -> None:
    # 少了第 7 天电话随访：这是红旗复核节点，不能放过
    verdict = rules.evaluate(
        "骨科术后",
        [node(3), node(30, "门诊复查", "outpatient")],
        evidence_ids=EVIDENCE,
    )
    assert verdict.passed is False
    assert "MISSING_REQUIRED_NODE" in codes(verdict)


def test_duplicate_offset_is_blocking() -> None:
    verdict = rules.evaluate(
        "骨科术后",
        [node(3), node(7, "电话随访", "phone"), node(30, "门诊复查"), node(30, "再次门诊")],
        evidence_ids=EVIDENCE,
    )
    assert verdict.passed is False
    assert "DUPLICATE_NODE" in codes(verdict)


def test_offset_out_of_range() -> None:
    verdict = rules.evaluate(
        "骨科术后",
        [node(3), node(7, "电话随访", "phone"), node(30), node(-3, "负数天"), node(9999, "太远")],
        evidence_ids=EVIDENCE,
    )
    assert verdict.passed is False
    assert "OFFSET_OUT_OF_RANGE" in codes(verdict)


def test_invalid_channel_is_warning_only() -> None:
    verdict = rules.evaluate(
        "骨科术后",
        [
            node(3),
            node(7, "电话随访", "phone"),
            node(30, "门诊复查", "outpatient"),
            node(15, "复查", "微信"),
        ],
        evidence_ids=EVIDENCE,
    )
    assert "CHANNEL_INVALID" in codes(verdict)
    # 渠道写错是"要核对"，不是"不许提交"
    assert verdict.passed is True


def test_unknown_profile_still_checks_structure() -> None:
    verdict = rules.evaluate("儿科哮喘随访", [node(7), node(30)], evidence_ids=EVIDENCE)
    assert verdict.profile_known is False
    assert "PROFILE_UNKNOWN" in codes(verdict)
    # 没有结构化规则时不做必检节点判定，但结构仍然要过
    assert "MISSING_REQUIRED_NODE" not in codes(verdict)


def test_empty_items_rejected() -> None:
    verdict = rules.evaluate("骨科术后", [], evidence_ids=EVIDENCE)
    assert verdict.passed is False
    assert "NO_ITEMS" in codes(verdict)


def test_no_evidence_never_passes() -> None:
    """没有引用到制度条文时，即使节点排得完全正确也不能给 pass。"""
    verdict = rules.evaluate(
        "骨科术后",
        [node(3), node(7, "电话随访", "phone"), node(30, "门诊复查", "outpatient")],
        evidence_ids=[],
    )
    assert verdict.passed is False
    assert "NO_POLICY_EVIDENCE" in codes(verdict)


def test_channel_mismatch_on_required_node() -> None:
    """必检节点明确要求电话，写成门诊只给警告，但要点出来。"""
    verdict = rules.evaluate(
        "骨科术后",
        [node(3), node(7, "电话随访", "outpatient"), node(30, "门诊复查", "outpatient")],
        evidence_ids=EVIDENCE,
    )
    assert "NODE_CHANNEL_MISMATCH" in codes(verdict)
