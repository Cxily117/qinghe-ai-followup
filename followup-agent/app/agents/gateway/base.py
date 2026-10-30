"""LLM 网关抽象层（★ 阶段 1 核心模块 ⑤ 的迁移目标）。

设计要点（对应技术架构方案「换供应商只改 config 不改代码」的验收标准）：
  - 上层（services / agents.rag）只依赖 LLMProvider 的 chat / stream 契约；
  - 新增供应商 = 新写一个 Provider 子类 + 在 get_llm() 注册一行；
  - 各 Provider 自行负责超时、重试、用量回传。

统一契约：
  chat(messages)  -> ChatResult(text, model, usage, latency_ms)
  stream(messages) -> AsyncIterator[str]，逐段吐出增量文本（不含协议包装）
  chat_tools(messages, tools) -> ChatResult(extra["tool_calls"] = [ToolCall, ...])

为什么把工具调用也收进网关（而不是在 D5 里直接写 httpx）：
  换供应商时「生成任务草稿」这条链路同样不该重写；而且 mock 也要能返回工具调用，
  否则离线环境下 D5 的整条落库链路就没法验证。
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


@dataclass
class ToolCall:
    """一次工具调用请求（已解析成 dict 参数）。"""

    name: str
    arguments: dict
    # 原始 arguments 字符串保留下来：模型给出非法 JSON 时，这是唯一的排障线索
    raw_arguments: str = ""
    call_id: str = ""


class ToolCallParseError(ValueError):
    """模型返回的工具参数不是合法 JSON / 不是 object。"""


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

    async def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | dict = "auto",
        temperature: float | None = None,
    ) -> ChatResult:
        """带工具的一次性调用，结果放在 `ChatResult.extra["tool_calls"]`。

        不设为抽象方法：只做问答的子类（未来的纯嵌入/纯摘要 Provider）不必实现它，
        调用方拿到 NotImplementedError 即可明确降级。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 chat_tools")


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
