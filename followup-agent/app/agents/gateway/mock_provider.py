"""Mock 供应商：离线联调用，不产生真实智能，但严格走同一条链路。

用途：
  - 没有 API Key 的同事 clone 下来就能跑通「检索 → 拼提示词 → 流式输出 → 落库」；
  - 单元测试里避免外网依赖与费用；
  - 演示「拒答护栏」：检索为空时回答固定的拒绝话术，并【不】标注引用。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

from app.agents.gateway.base import ChatResult, LLMProvider, ToolCall, timer_start
from app.agents.rules import match_profile

REFUSAL_TEXT = "该问题超出当前知识库范围，请查阅制度原文或咨询随访管理员。"

CREATE_DRAFT_NAME = "create_followup_task_draft"

# 三条脚本化路径的关键词（对应 docs/agent-trajectories.md 的三条典型轨迹）
_RED_FLAG_WORDS = (
    "胸痛", "胸闷", "气促", "呼吸困难", "发热", "发烧", "高热",
    "渗液", "红肿", "感染", "晕厥", "意识", "剧痛", "出血",
)
_CREATE_WORDS = ("新建", "创建", "录入", "建一份", "生成随访", "排一下", "安排随访")

# 患者标识：兼容「患者编号 P1234」「住院号：ZY2026001」「病人 ID 8891」
_PATIENT_RE = re.compile(
    r"(?:患者|病人|住院号|门诊号|床位号)\s*(?:编号|ID|id)?\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-_]{2,})"
)
# 随访节点：「第 3 天」「术后 7 天」「两周后」「1 个月」
_DAY_RE = re.compile(r"(?:第|术后|出院后)\s*(\d{1,3})\s*天|(\d{1,2})\s*周后|(\d{1,2})\s*个月")
_CHANNEL_HINTS = (("电话", "phone"), ("短信", "sms"), ("微信", "wecom"), ("门诊", "outpatient"))
_PLAN_HINTS = ("骨科", "关节", "髋", "膝", "脊柱", "肿瘤", "心内", "产科", "冠心病", "心梗")


def _route_of(question: str) -> str:
    """把问题归到三条脚本路径之一（mock 专用，真实模型不需要这个函数）。

    redflag 优先于 create：同样的句子里出现"胸痛"和"新建"，先救命再排程。
    """
    if any(w in question for w in _RED_FLAG_WORDS):
        return "redflag"
    if any(w in question for w in _CREATE_WORDS) and "随访" in question:
        return "create"
    return "qa"


def _called_tools(messages: list[dict]) -> set[str]:
    called: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                called.add((tc.get("function") or {}).get("name", ""))
    return called


def _last_tool_payload(messages: list[dict]) -> dict:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if not tool_msgs:
        return {}
    try:
        return json.loads(tool_msgs[-1].get("content") or "{}")
    except Exception:
        return {}


def _payload_with(messages: list[dict], *keys: str) -> dict:
    """按「包含哪些字段」回溯工具结果，而不是只看最后一条。

    踩过的坑：解析患者拿到 plan_type 后，下一轮又调了 search_policy，
    "最后一条结果"变成 hits，方案名就丢了，后续校验退化成默认档位。
    mock 的状态必须按键回溯，才不会被中间的检索打断。
    """
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        try:
            payload = json.loads(m.get("content") or "{}")
        except Exception:
            continue
        if all(k in payload for k in keys):
            return payload
    return {}


def _profile_items(question: str, plan_type: str) -> list[dict]:
    """按方案档位给出「制度要求的节点」，供 mock 演示合规校验与建草稿。

    刻意与 rules.PLAN_PROFILES 共用同一份常量：
    如果 mock 自己另写一套天数，演示就会"看起来通过、实际规则不通过"，
    这种假绿比没测更糟。
    """
    profile, _ = match_profile(plan_type)
    items: list[dict] = []
    for node in profile.required:
        items.append(
            {
                "name": node.name_hint,
                "offset_days": node.offset_days,
                "channel": node.channel or "phone",
                "notes": "依据制度必检节点",
            }
        )
    # 问题里显式给了天数就以医护说的为准（模拟"模型尊重对话原文"）
    explicit = []
    for day, week, month in _DAY_RE.findall(question):
        explicit.append(int(day) if day else int(week) * 7 if week else int(month) * 30)
    if explicit:
        items = [
            {
                "name": f"出院后第 {d} 天随访",
                "offset_days": d,
                "channel": "phone",
                "notes": "医护在对话中指定",
            }
            for d in sorted(set(explicit))
        ]
    return items[:10]


def _agent_next_step(
    messages: list[dict], user_content: str, tool_names: list[str]
) -> tuple[ToolCall | None, str]:
    """按三条预置路径推进 mock 的决策序列。

    它故意**不会**跳过校验直接建草稿：这样冒烟验证的是"顺序真的被执行了"，
    而不只是字段格式对不对。真实模型允许在骨架内自己选路，mock 只走最标准的那条。
    """
    called = _called_tools(messages)
    last = _last_tool_payload(messages)
    route = _route_of(user_content)
    patient = _PATIENT_RE.search(user_content)
    patient_ref = patient.group(1) if patient else ""

    def has(tool: str) -> bool:
        return tool in tool_names

    # ---------------- 轨迹 C：红旗症状优先，先救命再讲条文 ----------------
    if route == "redflag":
        if has("assess_red_flag") and "assess_red_flag" not in called:
            return (
                ToolCall(
                    name="assess_red_flag",
                    arguments={"symptoms": user_content[:200]},
                    raw_arguments="{}",
                ),
                "",
            )
        risk = _payload_with(messages, "level")
        if risk.get("level"):
            # 高危：给出处置动作 + 制度依据，不再排任何随访节点
            if has("search_policy") and "search_policy" not in called:
                return (
                    ToolCall(
                        name="search_policy",
                        arguments={"query": "红旗症状 处置 升级 发热 胸痛"},
                        raw_arguments="{}",
                    ),
                    "",
                )
            hits = _payload_with(messages, "hits").get("hits") or []
            level = "高危" if risk.get("level") == "high" else "需关注"
            actions = "；".join(
                h.get("required_action", "") for h in (risk.get("hits") or [])
            )
            cite = f"[{hits[0].get('n', 1)}]" if hits else ""
            return None, (
                f"（mock 模型·仅验证链路）这是{level}红旗症状，先按制度处置：{actions}。"
                f"请立即电话联系主管医生并记录接手人与时间；随访问卷暂停。{cite}"
                if actions
                else "（mock 模型）已标记红旗预警，请按制度立即联系主管医生。"
            )
        return None, "已记录症状，请补充患者住院号以便登记升级工单。"

    # ---------------- 轨迹 B：新建随访（解析→检索→校验→建草稿→等确认） ----------------
    if route == "create":
        if has("resolve_patient") and "resolve_patient" not in called:
            if not patient_ref:
                return None, (
                    "请提供患者的住院号或门诊号，我需要先在系统里定位到这位患者，"
                    "才能按制度校验随访节点。"
                )
            return (
                ToolCall(name="resolve_patient", arguments={"patient_ref": patient_ref}, raw_arguments="{}"),
                "",
            )
        if "resolve_patient" in called and last.get("found") is False:
            return None, f"没有在系统里找到这位患者（{last.get('reason', '未找到')}），请核对住院号后再试。"

        resolved = _payload_with(messages, "patient_id")
        plan_type = resolved.get("plan_type") or next(
            (f"{h}术后随访" for h in _PLAN_HINTS if h in user_content), "常规术后随访"
        )

        if has("search_policy") and "search_policy" not in called:
            return (
                ToolCall(
                    name="search_policy",
                    arguments={"query": f"{plan_type} 随访 时间 节点", "plan_type": plan_type},
                    raw_arguments="{}",
                ),
                "",
            )

        if has("check_followup_rule") and "check_followup_rule" not in called:
            return (
                ToolCall(
                    name="check_followup_rule",
                    arguments={
                        "patient_id": resolved.get("patient_id") or patient_ref,
                        "plan_type": plan_type,
                        "items": _profile_items(user_content, plan_type),
                    },
                    raw_arguments="{}",
                ),
                "",
            )

        # 校验结果按键回溯（不能只看最后一条：之后可能还有别的检索）
        check_payload = _payload_with(messages, "passed")

        if check_payload and check_payload.get("passed") is False:
            violations = "；".join(
                v.get("message", "") for v in (check_payload.get("violations") or [])[:3]
            )
            return None, (
                f"（mock 模型）按制度校验未通过：{violations}。"
                "请确认是否调整方案；我不会为了让校验通过而自行改动医护的安排。"
            )

        if has(CREATE_DRAFT_NAME) and CREATE_DRAFT_NAME not in called and check_payload.get("rule_ticket"):
            return (
                ToolCall(
                    name=CREATE_DRAFT_NAME,
                    arguments={
                        "rule_ticket": check_payload["rule_ticket"],
                        "patient_ref": patient_ref or last.get("patient_id") or "",
                        "plan_type": plan_type,
                        "items": _profile_items(user_content, plan_type),
                        "rationale": "依据制度必检节点与医护的原始描述",
                    },
                    raw_arguments="{}",
                ),
                "",
            )

        if CREATE_DRAFT_NAME in called and _payload_with(messages, "pending_confirm"):
            # 正常情况下 harness 会在 pending 之后立刻中断，走不到这里；
            # 保留兜底措辞，避免将来流程改动时模型"声称已创建"。
            return None, "草稿已生成，等待医护确认后才会写入系统。"

        return None, "已了解随访需求，请补充住院号与随访节点。"

    # ---------------- 轨迹 A：只查规范，不碰业务工具 ----------------
    if has("search_policy") and "search_policy" not in called:
        return (
            ToolCall(name="search_policy", arguments={"query": user_content[:80]}, raw_arguments="{}"),
            "",
        )

    hits = _last_tool_payload(messages).get("hits") or []
    if hits:
        first = hits[0]
        return None, (
            f"（mock 模型·仅验证链路）根据《{first.get('doc_title', '制度')}》相关条款："
            f"「{(first.get('content') or '')[:120]}……」[{first.get('n', 1)}]\n"
            "以上为制度要求的随访节点与内容，具体问询项以该条文为准。"
        )
    if "search_policy" in called:
        return None, REFUSAL_TEXT
    return None, "已了解您的需求，请补充患者住院号或具体问题。"


def _dialogue_only(user_content: str) -> str:
    """只取「医护：」之后的对话正文。

    拼好的用户消息末尾会带一行「出院日（基准日，第 0 天）：2026-10-20」，
    如果直接正则扫全文，会把这个 0 天当成一个随访节点抽出来——
    真实模型能区分这是元信息，正则不能，所以先切出对话区。
    """
    lines = re.findall(r"医护：(.*)", user_content)
    return "\n".join(lines) if lines else user_content


def _extract_first_source(user_content: str) -> str:
    """从拼好的 user message 里抽出【资料1】的正文，用于回显。"""
    match = re.search(r"【资料1】(.*?)(?:\n\n【资料|\n\n问题：|$)", user_content, re.S)
    return (match.group(1).strip() if match else "")


def _build_mock_answer(messages: list[dict]) -> str:
    user_content = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )
    source = _extract_first_source(user_content)
    if not source:
        return REFUSAL_TEXT

    preview = source[:80].replace("\n", " ")
    return (
        f"（mock 模型·仅验证链路，未接入真实大模型）根据制度资料，与本次提问相关的规定为："
        f"「{preview}……」[1]\n"
        f"如需完整条款，请查看引用的制度原文。"
    )


class MockProvider(LLMProvider):
    name = "mock"

    async def chat(self, messages: list[dict], temperature: float | None = None) -> ChatResult:
        started = timer_start()
        text = _build_mock_answer(messages)
        # 模拟一点点延迟，让前端的 loading / 流式动画有真实观感
        await asyncio.sleep(0.05)
        return ChatResult(
            text=text,
            model="mock",
            latency_ms=int((timer_start() - started) * 1000),
            usage=None,
        )

    async def stream(
        self, messages: list[dict], temperature: float | None = None
    ) -> AsyncIterator[str]:
        text = _build_mock_answer(messages)
        # 按 12 字一小段吐，模拟真实流式的分块粒度
        for i in range(0, len(text), 12):
            await asyncio.sleep(0.01)
            yield text[i : i + 12]

    # ---------------- 工具调用（D5 抽取 / 阶段 4 智能体）----------------

    async def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | dict = "auto",
        temperature: float | None = None,
    ) -> ChatResult:
        """用正则给出一份「像样的」工具入参，专门用来验证落库与护栏，不产生真实智能。

        刻意保留两个「坏行为」，让护栏能被真正测到：
          - 找不到患者标识时返回空字符串（不编造）→ 服务端应 422 拒绝；
          - 找不到任何随访节点时返回空 items → 服务端应 422 拒绝。
        """
        started = timer_start()
        user_content = _dialogue_only(
            next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        )
        tool_names = [t["function"]["name"] for t in tools]
        name = tools[0]["function"]["name"] if tools else ""

        await asyncio.sleep(0.05)

        # 智能体链路（多工具）：按预置路径推进；D5 的任务抽取只给 1 个工具，走下面老逻辑
        if tool_names != [CREATE_DRAFT_NAME] and tool_names:
            call, text = _agent_next_step(messages, user_content, tool_names)
            return ChatResult(
                text=text,
                model="mock",
                latency_ms=int((timer_start() - started) * 1000),
                usage=None,
                extra={
                    "tool_calls": [call] if call else [],
                    "finish_reason": "tool_calls" if call else "stop",
                    "degraded": None,
                },
            )

        patient = _PATIENT_RE.search(user_content)
        offsets: list[int] = []
        for day, week, month in _DAY_RE.findall(user_content):
            offsets.append(int(day) if day else int(week) * 7 if week else int(month) * 30)
        channel = next((v for k, v in _CHANNEL_HINTS if k in user_content), "phone")
        plan = next((f"{h}术后随访" for h in _PLAN_HINTS if h in user_content), "常规术后随访")

        arguments = {
            "patient_ref": patient.group(1) if patient else "",
            "plan_type": plan,
            "items": [
                {
                    "name": f"出院后第 {d} 天随访",
                    "offset_days": d,
                    "channel": channel,
                    "notes": "mock 抽取，仅验证链路",
                }
                for d in sorted(set(offsets))[:10]
            ],
            "rationale": "mock 依据对话中的天数与渠道关键词生成",
        }
        await asyncio.sleep(0.05)
        return ChatResult(
            text="",
            model="mock",
            latency_ms=int((timer_start() - started) * 1000),
            usage=None,
            extra={
                "tool_calls": [ToolCall(name=name, arguments=arguments, raw_arguments="{}")],
                "finish_reason": "tool_calls",
                "degraded": None,
            },
        )
