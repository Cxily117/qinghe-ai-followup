"""PHI 脱敏单测：审计里"能不能反查到人"完全取决于这几个函数。

三个落点分别验：
  - mask_ids        —— 住院号在文本里出现（审计入参、日志）
  - redact_args     —— 工具入参按 key 打码
  - redact_question —— 运行记录里存的问题原文（上线清单 S2）
"""

from __future__ import annotations

from app.agents.harness import mask_ids, redact_arg_value, redact_args, result_digest
from app.services.agent_service import redact_question


# ---------------------------- 住院号 ----------------------------
def test_mask_ids_keeps_prefix_only() -> None:
    masked = mask_ids("住院号 ZY2026001 已出院")
    assert "ZY2026001" not in masked
    assert masked.startswith("住院号 ZY")
    assert "*" in masked


def test_mask_ids_handles_multiple_and_empty() -> None:
    masked = mask_ids("ZY2026001 / P123456 / A0000123")
    assert "ZY2026001" not in masked
    assert "P123456" not in masked
    assert "A0000123" not in masked
    assert mask_ids("") == ""
    assert mask_ids(None) == ""


def test_redact_arg_value_keeps_edges() -> None:
    # 两个字没法"留首尾各一位"，整体打码才不会等于没脱敏
    assert redact_arg_value("张伟") == "**"
    masked = redact_arg_value("ZY2026001")
    assert masked[0] == "Z" and masked[-1] == "1"  # 首尾留着，人工比对用
    assert masked[1:-1].strip("*") == ""  # 中间全是星，反查不到
    assert redact_arg_value("") == ""


# ---------------------------- 工具入参 ----------------------------
def test_redact_args_masks_patient_fields() -> None:
    out = redact_args({"patient_ref": "ZY2026001", "name": "张伟", "symptoms": "发热三天"})
    assert out["patient_ref"] != "ZY2026001"
    assert out["name"] != "张伟"
    assert out["symptoms"] != "发热三天"


def test_redact_args_keeps_query_for_replay() -> None:
    """query 保留原文，否则回放时看不出"它到底查了什么"；但仍先过 ID 掩码。"""
    out = redact_args({"query": "ZY2026001 术后随访节点"})
    assert "术后随访节点" in out["query"]
    assert "ZY2026001" not in out["query"]


def test_redact_args_handles_none() -> None:
    assert redact_args(None) == {}


# ---------------------------- 运行记录问题 ----------------------------
def test_redact_question_masks_all_identifiers() -> None:
    q = "住院号 ZY2026001，电话 13800000000，身份证 110101199001011234，术后第 5 天能散步吗"
    masked = redact_question(q)
    assert "ZY2026001" not in masked
    assert "13800000000" not in masked
    assert "110101199001011234" not in masked
    # 业务语义要留住，否则回放看不懂在问什么
    assert "术后第 5 天能散步吗" in masked


def test_redact_question_keeps_plain_question() -> None:
    q = "关节置换术后制度要求哪些随访节点？"
    assert redact_question(q) == q


# ---------------------------- 结果摘要 ----------------------------
def test_result_digest_masks_nested_name() -> None:
    """工具返回是嵌套结构，姓名藏在里层——这里漏了等于审计表里躺着全名。"""
    payload = {
        "found": True,
        "patient": {"name": "张伟", "patient_ref": "ZY2026001", "dept": "骨科"},
    }
    d1, d2 = result_digest(payload), result_digest(payload)
    assert d1 == d2
    assert "张伟" not in d1
    assert "ZY2026001" not in d1
    # 结构要能看出来，否则回放失去意义
    assert "骨科" in d1
    assert len(d1) <= 250


def test_result_digest_changes_with_content() -> None:
    assert result_digest({"a": 1}) != result_digest({"a": 2})
