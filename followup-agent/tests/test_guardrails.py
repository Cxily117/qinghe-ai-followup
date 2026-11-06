"""输出护栏单测：这是"答案可信"的最后一道确定性关卡。

覆盖四类结论，每一类都对应一个真实踩过的坑：
  1. 引用编号必须在真实资料范围内（模型会编 [9]）；
  2. 拒答不得带引用（"用编号包装一个编造的答案"比少显示来源严重得多）；
  3. 命中资料就该给来源卡片，包括一句话的短答案（旧版按 60 字门槛会漏）；
  4. 条文里没有的时点，不能顺着"随访节奏"编出来。
"""

from __future__ import annotations

from app.agents.guardrails import (
    DISCLAIMER,
    apply_output_guardrails,
    contains_medical_advice,
    detect_node_extrapolation,
    extract_citations,
    has_red_flag,
    looks_like_refusal,
    strip_citations,
)


# ---------------------------- 引用解析 ----------------------------
def test_extract_citations_dedup_and_sort() -> None:
    assert extract_citations("结论[2]，补充[1]，又见[2]", 3) == [1, 2]


def test_extract_citations_drops_out_of_range() -> None:
    # 只有 2 条资料，[9] 是模型编的，不能进引用列表
    assert extract_citations("依据[1]与[9]", 2) == [1]


def test_source_mark_normalized_to_citation() -> None:
    # 「【资料2】」这种写法等价于 [2]
    answer, citations, _ = apply_output_guardrails("按制度要求【资料2】执行。", 3)
    assert citations == [2]
    assert "【资料2】" not in answer


def test_strip_citations_keeps_text() -> None:
    assert strip_citations("结论[1] 与 [2]") == "结论 与"


# ---------------------------- 拒答 ----------------------------
def test_refusal_detected() -> None:
    assert looks_like_refusal("该问题超出当前知识库范围。") is True
    assert looks_like_refusal("知识库中暂无相关制度。") is True


def test_refusal_clears_citations() -> None:
    """拒答却带编号 = 看起来像编造出处，必须清干净。"""
    answer, citations, flags = apply_output_guardrails("该问题超出当前知识库范围[1]。", 3)
    assert citations == []
    assert flags.get("refusal") is True
    assert "[1]" not in answer


def test_long_answer_is_not_mistaken_for_refusal() -> None:
    """长答案里顺带出现"无法回答"不该被整段判成拒答。"""
    long_text = "随访节点如下：" + "第7天电话随访；" * 12 + "超出本条无法回答的部分请咨询医生。"
    assert looks_like_refusal(long_text) is False


# ---------------------------- 来源卡片 ----------------------------
def test_short_answer_still_gets_citation_cards() -> None:
    """一句话结论同样要有来源——旧版按 60 字门槛会把这类短答案漏掉。"""
    answer, citations, flags = apply_output_guardrails("第 5 天加做一次电话复核。", 2)
    assert citations == [1, 2]
    assert flags.get("citations_attached") is True
    assert "未逐句标注" in answer


def test_self_written_source_block_removed() -> None:
    body = (
        "骨科关节置换术后，制度要求出院后第 3 天做伤口评估、第 7 天电话随访切口与疼痛、"
        "第 30 天门诊复查功能恢复情况；若出现发热或切口渗液，应提前联系随访护士。"
    )
    answer, _, flags = apply_output_guardrails(
        body + "[1]\n\n引用文档来源：\n- 《骨科术后随访规范》", 2
    )
    assert "引用文档来源" not in answer
    assert flags.get("source_block_stripped") is True


def test_no_source_means_uncited_flag() -> None:
    _, citations, flags = apply_output_guardrails(
        "术后一般建议多休息，注意伤口干燥，避免剧烈活动，饮食清淡，按医嘱服药，"
        "定期门诊复查；如出现发热、切口红肿或渗液，请及时联系随访护士，不要自行处理。",
        0,
    )
    assert citations == []
    assert flags.get("uncited_answer") is True
    assert flags.get("suspected_general_knowledge") is True


# ---------------------------- 节点外推 ----------------------------
def test_node_extrapolation_detected() -> None:
    hit = detect_node_extrapolation(
        "术后第 45 天要做什么？",
        ["术后第 3 天评估、第 7 天电话随访、第 30 天门诊复查。"],
        "第 45 天需要回院复查。",
    )
    assert hit is not None
    missing, covered = hit
    assert missing == [45]
    assert covered == [3, 7, 30]


def test_node_extrapolation_ignores_non_question() -> None:
    """「术后第 5 天想散步可以吗」是在描述现状，不是查节点，不该被拦。"""
    assert (
        detect_node_extrapolation(
            "术后第 5 天想散步可以吗", ["第 3 天、第 7 天随访。"], "可以适度活动。"
        )
        is None
    )


def test_node_extrapolation_blocks_answer() -> None:
    answer, citations, flags = apply_output_guardrails(
        "第 45 天需要回院复查。",
        2,
        question="术后第 45 天要做什么？",
        sources=["术后第 3 天评估、第 7 天电话随访、第 30 天门诊复查。"],
    )
    assert citations == []
    assert flags.get("extrapolation_blocked") is True
    assert "未查询到相关内容" in answer


# ---------------------------- 医疗建议与红旗 ----------------------------
def test_medical_advice_gets_disclaimer() -> None:
    answer, _, flags = apply_output_guardrails("建议服用地塞米松，剂量 5mg。", 1)
    assert flags.get("medical_advice_suspected") is True
    assert DISCLAIMER in answer


def test_medical_advice_detector() -> None:
    assert contains_medical_advice("布洛芬一次 2 片") is True
    assert contains_medical_advice("请按制度安排第 7 天电话随访") is False


def test_red_flag_keywords_cover_colloquial() -> None:
    # 护士会说"发烧 39 度"，不会说"发热"
    assert has_red_flag("患者发烧 39 度") is True
    assert has_red_flag("切口有点渗液") is True
    assert has_red_flag("今天天气不错") is False
