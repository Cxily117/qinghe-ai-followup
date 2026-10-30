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

from app.agents.gateway.base import (
    ChatResult,
    LLMProvider,
    ToolCall,
    ToolCallParseError,
    timer_start,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 允许重试的状态码：限流与上游故障
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

# 上游明确拒绝 tools 字段时返回的状态码：400/422 常见于「模型不支持 function calling」
TOOL_UNSUPPORTED_STATUS = {400, 404, 422}


def _loads_json_loose(raw: str) -> dict:
    """解析模型给的 JSON：容忍 ```json 代码块包裹与前后闲聊文字。

    真实模型（尤其是小参数模型）经常把工具参数写成代码块，或者先来一句
    「好的，我来整理：」。严格 json.loads 会直接炸，所以先剥壳再截取首尾花括号。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ToolCallParseError(f"模型未返回 JSON 对象：{text[:120]}")
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolCallParseError(f"工具参数不是合法 JSON（{exc}）：{text[:160]}") from exc
    if not isinstance(data, dict):
        raise ToolCallParseError(f"工具参数应为 JSON 对象，实际为 {type(data).__name__}")
    return data


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

    # ---------------- 工具调用（D5）----------------

    async def _post_json(self, url: str, body: dict) -> tuple[int, dict, str]:
        """带重试的 POST，返回 (status, json, text)。不抛 HTTP 状态异常，由调用方判定。"""
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    resp = await client.post(url, headers=self._headers(), json=body)
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = exc
                    if attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(0.6 * attempt)
                        continue
                    break
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                    logger.warning("LLM 返回 %s，第 %s 次重试", resp.status_code, attempt)
                    await asyncio.sleep(0.6 * attempt)
                    continue
                try:
                    return resp.status_code, resp.json(), resp.text
                except ValueError:
                    return resp.status_code, {}, resp.text
        raise RuntimeError(f"LLM 调用失败（已重试 {MAX_ATTEMPTS} 次）：{last_error}")

    async def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | dict = "auto",
        temperature: float | None = None,
    ) -> ChatResult:
        """调用 /chat/completions 的 tools 能力，解析出结构化的工具入参。"""
        started = timer_start()
        url = f"{self.base_url}/chat/completions"
        body = self._payload(messages, temperature, stream=False)
        body["tools"] = tools
        body["tool_choice"] = tool_choice
        body["parallel_tool_calls"] = False  # 只要一次抽取，避免模型返回多个互相冲突的草稿

        status, data, text = await self._post_json(url, body)
        if status in TOOL_UNSUPPORTED_STATUS:
            logger.warning("上游拒绝 tools 字段（%s），降级为 JSON 输出模式：%s", status, text[:200])
            return await self._chat_json_fallback(messages, tools, temperature, started)
        if status >= 400:
            raise RuntimeError(f"LLM 工具调用失败 {status}: {text[:300]}")

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            calls.append(
                ToolCall(
                    name=fn.get("name") or "",
                    arguments=_loads_json_loose(raw),
                    raw_arguments=raw[:4000],
                    call_id=tc.get("id") or "",
                )
            )
        return ChatResult(
            text=message.get("content") or "",
            model=data.get("model", self.model),
            latency_ms=int((timer_start() - started) * 1000),
            usage=data.get("usage"),
            extra={
                "tool_calls": calls,
                "finish_reason": choice.get("finish_reason"),
                "degraded": None,
            },
        )

    async def _chat_json_fallback(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float | None,
        started: float,
    ) -> ChatResult:
        """工具能力不可用时的降级路径：要求模型直接吐 JSON，再复用同一套解析。

        为什么值得写这一段：院内部署常换成本地小模型（vLLM 上的 Qwen 7B 等），
        它们对 tools 的支持时好时坏。没有降级，换模型就等于 D5 整条链路不可用。
        """
        fn = tools[0]["function"]
        schema_hint = json.dumps(fn["parameters"], ensure_ascii=False)
        patched = list(messages) + [
            {
                "role": "system",
                "content": (
                    "上游不支持工具调用。请只输出一个 JSON 对象，字段遵循以下 JSON Schema；"
                    "不要输出解释文字，不要输出代码块标记。\n" + schema_hint
                ),
            }
        ]
        url = f"{self.base_url}/chat/completions"
        body = self._payload(patched, temperature, stream=False)
        body["response_format"] = {"type": "json_object"}

        status, data, text = await self._post_json(url, body)
        if status in TOOL_UNSUPPORTED_STATUS:  # 连 response_format 都不支持：去掉再试一次
            body.pop("response_format", None)
            status, data, text = await self._post_json(url, body)
        if status >= 400:
            raise RuntimeError(f"LLM JSON 降级调用失败 {status}: {text[:300]}")

        message = ((data.get("choices") or [{}])[0]).get("message") or {}
        content = message.get("content") or ""
        return ChatResult(
            text=content,
            model=data.get("model", self.model),
            latency_ms=int((timer_start() - started) * 1000),
            usage=data.get("usage"),
            extra={
                "tool_calls": [
                    ToolCall(
                        name=fn["name"],
                        arguments=_loads_json_loose(content),
                        raw_arguments=content[:4000],
                    )
                ],
                "finish_reason": "tool_calls",
                "degraded": "json_fallback",
            },
        )
