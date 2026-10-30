"""Mock 供应商：离线联调用，不产生真实智能，但严格走同一条链路。

用途：
  - 没有 API Key 的同事 clone 下来就能跑通「检索 → 拼提示词 → 流式输出 → 落库」；
  - 单元测试里避免外网依赖与费用；
  - 演示「拒答护栏」：检索为空时回答固定的拒绝话术，并【不】标注引用。
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from app.agents.gateway.base import ChatResult, LLMProvider, ToolCall, timer_start

REFUSAL_TEXT = "该问题超出当前知识库范围，请查阅制度原文或咨询随访管理员。"

# 患者标识：兼容「患者编号 P1234」「住院号：ZY2026001」「病人 ID 8891」
_PATIENT_RE = re.compile(
    r"(?:患者|病人|住院号|门诊号|床位号)\s*(?:编号|ID|id)?\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-_]{2,})"
)
# 随访节点：「第 3 天」「术后 7 天」「两周后」「1 个月」
_DAY_RE = re.compile(r"(?:第|术后|出院后)\s*(\d{1,3})\s*天|(\d{1,2})\s*周后|(\d{1,2})\s*个月")
_CHANNEL_HINTS = (("电话", "phone"), ("短信", "sms"), ("微信", "wecom"), ("门诊", "outpatient"))
_PLAN_HINTS = ("骨科", "关节", "髋", "膝", "脊柱", "肿瘤", "心内", "产科")


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

    # ---------------- 工具调用（D5）----------------

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
        name = tools[0]["function"]["name"] if tools else ""

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
