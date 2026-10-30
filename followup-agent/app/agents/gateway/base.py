"""LLM 网关抽象层（★ 阶段 1 核心模块 ⑤ 的迁移目标）。

设计要点（对应技术架构方案「换供应商只改 config 不改代码」的验收标准）：
  - 上层（services / agents.rag）只依赖 LLMProvider 的 chat / stream 契约；
  - 新增供应商 = 新写一个 Provider 子类 + 在 get_llm() 注册一行；
  - 各 Provider 自行负责超时、重试、用量回传。

统一契约：
  chat(messages)  -> ChatResult(text, model, usage, latency_ms)
  stream(messages) -> AsyncIterator[str]，逐段吐出增量文本（不含协议包装）
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.core.config import get_settings


@dataclass
class ChatResult:
    """一次非流式调用的完整结果。"""

    text: str
    model: str
    latency_ms: int = 0
    # OpenAI 兼容接口回传的 usage：{"prompt_tokens":..,"completion_tokens":..,"total_tokens":..}
    # 部分国产接口不回传，则为 None（前端展示时不要假定存在）
    usage: dict | None = None
    extra: dict = field(default_factory=dict)


class LLMProvider(ABC):
    """所有供应商实现的基类。"""

    name: str = "base"

    @abstractmethod
    async def chat(self, messages: list[dict], temperature: float | None = None) -> ChatResult:
        """一次性返回完整回答。"""

    @abstractmethod
    def stream(
        self, messages: list[dict], temperature: float | None = None
    ) -> AsyncIterator[str]:
        """流式返回增量文本。注意：这是普通函数，返回 async generator。"""


def get_llm() -> LLMProvider:
    """供应商工厂：由 settings.llm_provider 决定，业务代码不感知具体实现。

    mock  —— 离线联调，无 Key 也能跑通「检索→拼提示词→流式输出→落库」全链路
    openai —— 任何 OpenAI 兼容接口（智谱 GLM / DeepSeek / 通义 / 本地 vLLM 均可）
    """
    s = get_settings()
    provider = (s.llm_provider or "mock").lower()

    if provider == "mock":
        from app.agents.gateway.mock_provider import MockProvider

        return MockProvider()

    if provider in {"openai", "zhipu", "glm", "deepseek", "qwen", "compatible"}:
        from app.agents.gateway.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider()

    raise ValueError(
        f"未知 LLM_PROVIDER={s.llm_provider}，可选：mock / openai（OpenAI 兼容任意供应商）"
    )


def timer_start() -> float:
    """统一的计时起点（毫秒计时用它，避免各 Provider 各写一遍）。"""
    return time.perf_counter()
