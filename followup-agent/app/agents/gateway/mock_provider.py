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

from app.agents.gateway.base import ChatResult, LLMProvider, timer_start

REFUSAL_TEXT = "该问题超出当前知识库范围，请查阅制度原文或咨询随访管理员。"


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
