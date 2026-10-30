"""输出护栏（★ 制度问答的合规收口，对应需求清单「硬关卡」原则）。

为什么护栏要做在代码里，而不是只写在提示词里：
  提示词是「请求」，代码是「保证」。模型可能被诱导、可能升级后行为漂移，
  而医疗场景下「拒答却标了引用」「给了用药建议」这类问题不能靠模型自觉。
  所以这里对模型输出做一次确定性的后处理：
    1. 引用编号必须落在真实资料范围内，越界的 [9] 直接删掉（防幻觉引用）；
    2. 判定为拒答时，强制清除所有引用标记（提示词规则 3 的兜底）；
    3. 命中用药/剂量表述时，追加固定免责声明，并打 flag 供质控抽查；
    4. 检索资料命中红旗症状时，打 red_flag 标记，便于质控报表统计「AI 是否提示升级」。
"""
from __future__ import annotations

import re

REFUSAL_PATTERNS = (
    r"超出(当前)?知识库(的)?范围",
    r"知识库(中)?(暂无|没有|未收录|尚未)",
    r"资料(中|内)?(未|没有)(提及|涉及|包含|记载)",
    r"无法(从|根据)(资料|知识库)",
    r"(无法|不能)回答(该|这个|此)?问题",
)

# 拒答话术通常很短。加长度上限是为了避免「长答案里顺带出现『无法回答』」
# 就被整段判定为拒答、反而把正常引用的编号全删掉。
REFUSAL_MAX_LEN = 80

# 只做「提示性」检测，不做语义判定：命中即打标 + 追加免责，绝不改写模型的医学结论
MEDICAL_ADVICE_PATTERNS = (
    r"建议(服用|口服|使用|注射)",
    r"剂量",
    r"\d+\s*(mg|毫克|ml|毫升|片|粒)",
    r"停药",
    r"加量",
    r"减量",
)

# 红旗症状关键词：用于给检索结果打标（与制度里「红旗症状」章节对应）
# 口语说法要一并收进来：护士提问会说"发烧 39 度"，不会说"发热"；
# 只收书面语会让最需要预警的那类提问漏标。
RED_FLAG_KEYWORDS = (
    "发热", "发烧", "高烧", "体温", "38.5", "38度", "39度", "寒战",
    "切口", "渗液", "红肿", "意识", "剧痛", "胸闷",
)

DISCLAIMER = "（以上为制度条文摘要，不构成诊疗建议；具体处置请以专科医生意见为准。）"

# ---- 节点外推拦截 ----
# 真实模型（glm-4-flash）在 mock 时代暴露不出的问题上会翻车：问「第 45 天要做什么」，
# 条文里只写了 3/7/30 天，它会顺着"随访节奏"编出一套看起来很像的答案，还标着引用。
# 这类幻觉最危险——格式完全合规，只有懂业务的人才能看出时点是编的。
# 所以这里做确定性判定：问题明确问某天，而所有检索到的条文里都没有这个天数 → 强制拒答。
_NODE_IN_QUESTION = re.compile(r"(?:术后|出院后|第)\s*(\d{1,3})\s*(?:天|日)")
_NODE_IN_SOURCE = re.compile(r"(\d{1,3})\s*(?:天|日)")
# 只有"问询型"提问才拦：问「哪些/需要/怎么安排」是查制度；
# 「术后第 5 天想散步可以吗」是在描述患者现状，不该被当成查节点。
_ASK_HINTS = (
    "哪些", "什么", "几次", "几个", "项目", "内容", "需要", "安排",
    "做什么", "怎么", "何时", "什么时候", "多久", "几天",
)

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")

# 模型有时不用 [2] 而写成「根据资料【资料2】」——语义上就是引用，先归一化再解析，
# 否则会把一条有效引用判定成「无引用回答」，白打一个风险标签。
_SOURCE_MARK_RE = re.compile(r"【资料\s*(\d{1,2})\s*】")


def extract_citations(answer: str, n_sources: int) -> list[int]:
    """解析回答里实际出现的 [n]，过滤越界编号，去重并升序返回。"""
    found = {int(m) for m in _CITATION_RE.findall(answer)}
    return sorted(n for n in found if 1 <= n <= n_sources)


def looks_like_refusal(answer: str) -> bool:
    """判断模型是否在拒答（而不是在回答）。

    只认「明确的拒答话术」，且要求整段话足够短——早先版本用
    「长度 < 25 且无引用」兜底，把「建议服用地塞米松，剂量 5mg」这种
    极短的用药建议误判成拒答，导致最该追加免责的回答反而被放过，已移除该启发式。

    已知取舍：若模型「答一半 + 拒一半」，引用也会被清掉。这是故意选的偏向——
    在医疗场景里，「拒答却带着资料编号」看起来像编造出处，比「少显示两条来源」严重得多。
    宁可多清一次引用，也不让拒答带着编号出门。
    """
    text = answer.strip()
    if not text or len(text) > REFUSAL_MAX_LEN:
        return False
    return any(re.search(p, text) for p in REFUSAL_PATTERNS)


def strip_citations(text: str) -> str:
    """去掉引用标记，并清理多余空格。"""
    cleaned = _CITATION_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def has_red_flag(text: str) -> bool:
    """资料或问题里是否含红旗症状关键词。"""
    return any(kw in text for kw in RED_FLAG_KEYWORDS)


def contains_medical_advice(text: str) -> bool:
    return any(re.search(p, text) for p in MEDICAL_ADVICE_PATTERNS)


def detect_node_extrapolation(
    question: str, sources: list[str], answer: str
) -> tuple[list[int], list[int]] | None:
    """检测「问的时点在条文里根本不存在，模型却答了」。

    返回 (缺失的节点列表, 条文中实际覆盖的节点列表)，不需要拦截时返回 None。

    判定逻辑刻意保守（宁可少拦，不可误伤临床提问）：
      1. 问题里没出现「术后/出院后/第 N 天」→ 不拦；
      2. 不是问询型（没有"哪些/需要/怎么"等）→ 不拦，如"术后第 5 天想散步可以吗"；
      3. 每个被问到的天数都能在条文里找到 → 不拦。
    """
    if not question or not sources:
        return None
    if not any(h in question for h in _ASK_HINTS):
        return None

    asked = {int(n) for n in _NODE_IN_QUESTION.findall(question)}
    if not asked:
        return None

    blob = "\n".join(sources)
    covered = {int(n) for n in _NODE_IN_SOURCE.findall(blob)}
    missing = sorted(asked - covered)
    if not missing:
        return None
    # 只在"答案确实在讲这些时点"时拦；答案本身已拒答则不用管（上面已处理）
    return missing, sorted(covered)


def apply_output_guardrails(
    answer: str,
    n_sources: int,
    *,
    question: str = "",
    sources: list[str] | None = None,
) -> tuple[str, list[int], dict]:
    """对模型输出做确定性收口。

    参数：
        answer    模型原文
        n_sources 检索到的资料条数（引用编号上限）
        question  用户原始问题（可选：用于节点外推拦截）
        sources   检索到的条文正文列表（可选：同上）

    返回 (清洗后的回答, 有效引用编号列表, 供 meta 落库的 flags)。
    """
    flags: dict = {}

    # 先把「【资料2】」这种写法归一化成「[2]」，保证引用识别口径一致
    answer = _SOURCE_MARK_RE.sub(r"[\1]", answer)

    if looks_like_refusal(answer):
        # 规则 3 兜底：拒答就不能有引用，否则等于「用资料编号包装了一个编造的答案」
        cleaned = strip_citations(answer)
        flags["refusal"] = True
        return cleaned or "该问题超出当前知识库范围。", [], flags

    # 节点外推拦截（优先级高于引用清洗：编出来的答案不该带着引用出门）
    extrapolated = detect_node_extrapolation(question, sources or [], answer)
    if extrapolated:
        nodes, covered = extrapolated
        detail = "、".join(f"第{n}天" for n in nodes)
        covered_text = (
            "已发布制度中可查到的节点为 " + "、".join(f"第{n}天" for n in covered) + "。"
            if covered
            else ""
        )
        flags["refusal"] = True
        flags["node_not_in_policy"] = nodes
        flags["extrapolation_blocked"] = True
        return (
            f"关于{detail}的随访安排，随访规范中未查询到相关内容。"
            f"{covered_text}本系统只按制度条文回答，不对未列明的时点做推断；"
            "如需确认，请查阅制度原文或联系随访管理员。",
            [],
            flags,
        )

    citations = extract_citations(answer, n_sources)
    dropped = sorted({int(m) for m in _CITATION_RE.findall(answer)} - set(citations))
    if dropped:
        # 越界引用（模型编了不存在的资料编号）：删掉标记，并留痕
        answer = _CITATION_RE.sub(
            lambda m: m.group(0) if int(m.group(1)) in citations else "", answer
        )
        flags["dropped_citations"] = dropped

    # 「有实质内容却没引用任何资料」= 模型在靠自身知识作答，制度问答里这是高风险行为。
    # 不擅自删改内容（可能把有用信息删没），但必须打标，供质控抽查与前端提示。
    if not citations and len(answer) >= 60:
        flags["uncited_answer"] = True
        flags["suspected_general_knowledge"] = True

    if contains_medical_advice(answer):
        flags["medical_advice_suspected"] = True
        if DISCLAIMER not in answer:
            answer = f"{answer.rstrip()}\n\n{DISCLAIMER}"

    return answer, citations, flags
