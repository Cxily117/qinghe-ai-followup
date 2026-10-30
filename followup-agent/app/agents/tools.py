"""function calling 工具定义（★ D5 的核心契约）。

为什么工具入参用 `offset_days` 而不是绝对日期：
  大模型算日期是最不可靠的能力之一——「出院后第 7 天」它经常算出错误的日期，
  而且一旦上游把它写成 `2026-13-45` 这种值，解析层还得兜。
  所以让模型只输出「相对出院日的第几天」这个它最擅长的整数，
  绝对日期由服务端用 base_date + timedelta 算，且统一归一到 09:00。

为什么工具 schema 里【没有】status 字段：
  这是「AI 只能写草稿」这条硬关卡的物理实现——模型连表达「已确认」的语法都没有，
  比在提示词里写「不要自动确认」可靠得多。确认动作只能由人调 /tasks/{id}/confirm 触发。
"""
from __future__ import annotations

CREATE_TASK_DRAFT = {
    "type": "function",
    "function": {
        "name": "create_followup_task_draft",
        "description": (
            "把医护在对话中描述的随访安排，抽取成一张待人工确认的随访任务草稿。"
            "只抽取对话中明确出现的信息，不要自行补充随访方案。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
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
            "required": ["patient_ref", "items"],
        },
    },
}

TOOL_REGISTRY: dict[str, dict] = {
    CREATE_TASK_DRAFT["function"]["name"]: CREATE_TASK_DRAFT,
}

CREATE_TASK_DRAFT_NAME = CREATE_TASK_DRAFT["function"]["name"]
