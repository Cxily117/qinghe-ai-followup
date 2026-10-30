"""function calling 工具定义（D5 的任务草稿 + 阶段 4 的只读能力）。

两个贯穿始终的设计约束：

**① 入参用 `offset_days` 而不是绝对日期**
  大模型算日期是最不可靠的能力之一——「出院后第 7 天」它经常算出错误的日期，
  而且一旦上游把它写成 `2026-13-45` 这种值，解析层还得兜。
  所以让模型只输出「相对出院日的第几天」这个它最擅长的整数，
  绝对日期由服务端用 base_date + timedelta 算，且统一归一到 09:00。

**② 写操作的工具 schema 里【没有】status 字段**
  这是「AI 只能写草稿」这条硬关卡的物理实现——模型连表达「已确认」的语法都没有，
  比在提示词里写「不要自动确认」可靠得多。确认动作只能由人调 /tasks/{id}/confirm 触发。

阶段 4 的增量：工具不只是 schema，还带 `ToolSpec` 元数据（分类 / 解锁条件 / 单轮次数上限）。
下发哪几个由 `runtime.available_schemas()` 按运行状态决定，**没解锁的工具模型看不见**。
"""
from __future__ import annotations

from app.agents.runtime import (
    HANDOFF,
    JUDGE,
    READ,
    WRITE,
    RunState,
    ToolSpec,
)

CREATE_TASK_DRAFT = {
    "type": "function",
    "function": {
        "name": "create_followup_task_draft",
        "description": (
            "把医护在对话中描述的随访安排，抽取成一张待人工确认的随访任务草稿。"
            "只抽取对话中明确出现的信息，不要自行补充随访方案。"
            "调用前必须先通过 check_followup_rule 校验并拿到 rule_ticket。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rule_ticket": {
                    "type": "string",
                    "description": "check_followup_rule 返回的 rule_ticket 原文，服务端据此证明你已校验。",
                },
                "patient_ref": {
                    "type": "string",
                    "description": "对话中出现的患者标识（住院号/门诊号/患者编号）。对话中没有就留空字符串。",
                },
                "plan_type": {
                    "type": "string",
                    "description": "随访方案名称，如「骨科术后随访」「髋关节置换术后随访」。不确定填「常规术后随访」。",
                },
                "items": {
                    "type": "array",
                    "description": "随访节点清单，按时间从早到晚排序，最多 10 条。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "随访项名称，如「电话随访+切口评估」"},
                            "offset_days": {
                                "type": "integer",
                                "description": "相对出院日（第 0 天）的天数，取值 0-365",
                            },
                            "channel": {
                                "type": "string",
                                "enum": ["phone", "sms", "wecom", "outpatient"],
                                "description": "随访方式：电话/短信/企业微信/门诊复诊",
                            },
                            "notes": {"type": "string", "description": "该节点的注意事项，可为空"},
                        },
                        "required": ["name", "offset_days"],
                    },
                },
                "rationale": {
                    "type": "string",
                    "description": "一句话说明这份安排依据对话中的哪句话得出，便于人工核对。",
                },
            },
            "required": ["rule_ticket", "patient_ref", "items"],
        },
    },
}

RESOLVE_PATIENT = {
    "type": "function",
    "function": {
        "name": "resolve_patient",
        "description": (
            "按住院号/门诊号/患者编号解析院内患者，拿到出院日、科室与随访方案。"
            "查不到时返回 found=false，此时不得猜测患者信息，应向医护追问。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_ref": {"type": "string", "description": "对话中出现的患者标识原文"},
            },
            "required": ["patient_ref"],
        },
    },
}

SEARCH_POLICY = {
    "type": "function",
    "function": {
        "name": "search_policy",
        "description": (
            "在已发布的随访制度中检索条文。返回带编号的资料片段，"
            "编号可直接用作回答里的 [n] 引用。可以换关键词多次检索，最多 3 次。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，尽量用制度里的书面说法"},
                "plan_type": {"type": "string", "description": "限定病种/方案，可留空"},
            },
            "required": ["query"],
        },
    },
}

LIST_EXISTING_TASKS = {
    "type": "function",
    "function": {
        "name": "list_existing_tasks",
        "description": "查询该患者已经排过的随访节点，避免重复排程与漏排。",
        "parameters": {
            "type": "object",
            "properties": {"patient_id": {"type": "string", "description": "resolve_patient 返回的 patient_id"}},
            "required": ["patient_id"],
        },
    },
}

HANDOFF_TO_HUMAN = {
    "type": "function",
    "function": {
        "name": "handoff_to_human",
        "description": "当前问题超出制度与能力范围，转交人工处理。调用后不要再自行作答。",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "为什么无法处理，一句话说明"},
            },
            "required": ["reason"],
        },
    },
}

CHECK_RULE = {
    "type": "function",
    "function": {
        "name": "check_followup_rule",
        "description": (
            "用院内制度校验一组随访节点是否合规。返回 passed（由服务端依据条文计算，"
            "你无权自行判定）、violations 明细，以及 passed=true 时签发的 rule_ticket。"
            "rule_ticket 是创建草稿的唯一凭证，过期或跨患者使用都会被拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "resolve_patient 返回的 patient_id"},
                "plan_type": {"type": "string", "description": "随访方案名称"},
                "items": {
                    "type": "array",
                    "description": "待校验的随访节点，字段与创建草稿时一致",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "offset_days": {"type": "integer"},
                            "channel": {"type": "string"},
                        },
                        "required": ["name", "offset_days"],
                    },
                },
            },
            "required": ["patient_id", "items"],
        },
    },
}

ASSESS_RED_FLAG = {
    "type": "function",
    "function": {
        "name": "assess_red_flag",
        "description": (
            "评估症状描述是否命中红旗症状，返回风险等级与制度要求的处置动作。"
            "命中 high 时返回 risk_ticket，可用它调用 escalate_to_clinician。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string", "description": "resolve_patient 返回的 patient_id"},
                "symptoms": {"type": "string", "description": "医护描述的症状原文，不要改写"},
            },
            "required": ["symptoms"],
        },
    },
}

ESCALATE = {
    "type": "function",
    "function": {
        "name": "escalate_to_clinician",
        "description": "为红旗症状或制度不合规创建升级工单，交由主管医生处理。必须持 risk_ticket 或 rule_ticket。",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {"type": "string", "description": "risk_ticket 或 rule_ticket 原文"},
                "reason": {"type": "string", "description": "升级原因，须写明命中了哪条制度/哪个症状"},
            },
            "required": ["ticket", "reason"],
        },
    },
}


def _has_patient(state: RunState) -> bool:
    return bool(state.patient_id)


def _has_rule_ticket(state: RunState) -> bool:
    return bool(state.rule_ticket)


def _has_risk_ticket(state: RunState) -> bool:
    return bool(state.risk_ticket)


# 单轮检索次数上限：允许模型改写 query 重查，但不能无限跑
_SEARCH_LIMIT = lambda state: state.counters.get("search_policy", 0) < 3  # noqa: E731


TOOL_SPECS: dict[str, ToolSpec] = {
    "resolve_patient": ToolSpec(
        name="resolve_patient",
        schema=RESOLVE_PATIENT,
        category=READ,
        label="解析患者",
    ),
    "search_policy": ToolSpec(
        name="search_policy",
        schema=SEARCH_POLICY,
        category=READ,
        unlock=_SEARCH_LIMIT,
        label="检索随访制度",
    ),
    "list_existing_tasks": ToolSpec(
        name="list_existing_tasks",
        schema=LIST_EXISTING_TASKS,
        category=READ,
        unlock=_has_patient,
        label="查询已排随访",
    ),
    "assess_red_flag": ToolSpec(
        name="assess_red_flag",
        schema=ASSESS_RED_FLAG,
        category=READ,
        unlock=_has_patient,
        label="红旗症状评估",
    ),
    "check_followup_rule": ToolSpec(
        name="check_followup_rule",
        schema=CHECK_RULE,
        category=JUDGE,
        unlock=_has_patient,
        label="校验随访合规性",
    ),
    "create_followup_task_draft": ToolSpec(
        name="create_followup_task_draft",
        schema=CREATE_TASK_DRAFT,
        category=WRITE,
        # ★ 顺序约束的物理实现：没有 rule_ticket 时，这个工具根本不下发
        unlock=_has_rule_ticket,
        ticket_kind="rule",
        label="创建随访草稿",
    ),
    "escalate_to_clinician": ToolSpec(
        name="escalate_to_clinician",
        schema=ESCALATE,
        category=WRITE,
        unlock=lambda s: _has_risk_ticket(s) or _has_rule_ticket(s) or s.called("check_followup_rule"),
        ticket_kind="risk",
        label="升级主管医生",
    ),
    "handoff_to_human": ToolSpec(
        name="handoff_to_human",
        schema=HANDOFF_TO_HUMAN,
        category=HANDOFF,
        label="转人工",
    ),
}

# P1 只开放只读链路：写工具即使条件满足也不下发，确保「问答不退化、写链路零风险」
P1_ENABLED = ("resolve_patient", "search_policy", "list_existing_tasks", "handoff_to_human")

# 兼容 D5 旧调用点
TOOL_REGISTRY: dict[str, dict] = {name: spec.schema for name, spec in TOOL_SPECS.items()}
CREATE_TASK_DRAFT_NAME = "create_followup_task_draft"


def available_specs(state: RunState, *, enabled: tuple[str, ...] | None = None) -> list[ToolSpec]:
    """按运行状态下发工具集：未解锁的、超出次数的不出现。"""
    allowed = enabled or P1_ENABLED
    specs: list[ToolSpec] = []
    for name in allowed:
        spec = TOOL_SPECS.get(name)
        if spec is None:
            continue
        try:
            if not spec.unlock(state):
                continue
        except Exception:  # 解锁条件本身写错时，宁可不下发也不要放行写操作
            continue
        specs.append(spec)
    return specs


def available_schemas(state: RunState, *, enabled: tuple[str, ...] | None = None) -> list[dict]:
    return [spec.schema for spec in available_specs(state, enabled=enabled)]
