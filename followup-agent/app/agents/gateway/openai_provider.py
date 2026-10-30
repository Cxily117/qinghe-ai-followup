"""OpenAI 兼容供应商实现（智谱 GLM / DeepSeek / 通义 / 本地 vLLM 均适用）。

为什么只写一个「OpenAI 兼容」实现就够：
  国内主流大模型与院内私有化推理框架（vLLM / Xinference）都暴露
  /chat/completions 的 OpenAI 协议，换厂商只是换 base_url + model 名。

关键实现点：
  1. 统一走 httpx.AsyncClient，流式用 aiter_lines 解析 SSE；
  2. 失败重试只针对「可重试错误」（超时/连接失败/429/5xx），避免把用户输入错误重试放大；
  3. usage 有就回传、没有就 None——不假装有数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from app.agents.gateway.base import ChatResult, LLMProvider, timer_start
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 允许重试的状态码：限流与上游故障
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(self) -> None:
        s = get_settings()
        self.base_url = (s.llm_api_base or "https://api.openai.com/v1").rstrip("/")
        self.api_key = s.llm_api_key
        self.model = s.llm_chat_model
        self.timeout = s.llm_timeout

    # ---------------- 内部工具 ----------------

    def _headers(self) -> dict:
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY 未配置：请设置环境变量或在 .env 中填写"
                "（离线联调可设 LLM_PROVIDER=mock）"
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, messages: list[dict], temperature: float | None, stream: bool) -> dict:
        s = get_settings()
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": s.llm_temperature if temperature is None else temperature,
            "stream": stream,
        }
        if stream:
            # 让上游在最后一个 chunk 带回 usage；不支持的厂商会忽略该字段
            body["stream_options"] = {"include_usage": True}
        return body

    # ---------------- 非流式 ----------------

    async def chat(self, messages: list[dict], temperature: float | None = None) -> ChatResult:
        started = timer_start()
        url = f"{self.base_url}/chat/completions"
        body = self._payload(messages, temperature, stream=False)

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    resp = await client.post(url, headers=self._headers(), json=body)
                    if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                        logger.warning("LLM 返回 %s，第 %s 次重试", resp.status_code, attempt)
                        await asyncio.sleep(0.6 * attempt)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    return ChatResult(
                        text=data["choices"][0]["message"]["content"] or "",
                        model=data.get("model", self.model),
                        latency_ms=int((timer_start() - started) * 1000),
                        usage=data.get("usage"),
                    )
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = exc
                    if attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(0.6 * attempt)
                        continue
        raise RuntimeError(f"LLM 调用失败（已重试 {MAX_ATTEMPTS} 次）：{last_error}")

    # ---------------- 流式 ----------------

    async def stream(
        self, messages: list[dict], temperature: float | None = None
    ) -> AsyncIterator[str]:
        """逐段 yield 增量文本（已剥离 SSE 协议壳）。"""
        url = f"{self.base_url}/chat/completions"
        body = self._payload(messages, temperature, stream=True)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "ignore")
                    raise RuntimeError(f"LLM 流式调用失败 {resp.status_code}: {raw[:300]}")

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload in {"[DONE]", ""}:
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        # 个别厂商会插入心跳/注释行，跳过即可
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
