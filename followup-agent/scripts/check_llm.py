# -*- coding: utf-8 -*-
"""模型 Key 一键体检：换 Key、改模型、报「大模型连接失败」时先跑这个。

为什么要有它：
  「大模型连接失败」有六七种完全不同的成因（Key 填错/额度耗尽/模型名不存在/
  base 多了个 /v1/没实名/超时），在页面上报出来都是同一句红字。逐个翻日志猜很费时间，
  所以这里把三件事压成一条命令，5 秒出结论：
    1. 配置层：provider / base / model / Key 是否就位（Key 只显示前 6 位与长度，绝不回显全文）
    2. 对话层：真发一句最短的 chat 请求，拿到答复 = Key 与网络都通，顺带打延迟
    3. 向量层：真算一条 embedding，校验返回维度是否等于 EMBED_DIM
       （维度不符不会报错只会「检索到错条文」，这里提前拦住）

用法：
    python scripts/check_llm.py              # 检查 .env 当前生效配置
    python scripts/check_llm.py --verbose    # 顺带打印模型答复原文与 usage
退出码：0 = 对话与向量都通；1 = 有项目失败（可直接挂 CI 或换 Key 后的验收脚本）。

注意：脚本会真实消耗几 token 的额度（一次 chat + 一次 embedding，可忽略不计）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

# 允许 `python scripts/check_llm.py` 直接跑（不装包也能 import app.*）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.gateway.base import get_llm  # noqa: E402
from app.agents.gateway.embedding import EmbeddingClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"

# 状态码 → 人话。国内平台（智谱/DeepSeek/通义）在这一点上高度一致。
STATUS_HINTS = {
    400: "请求被拒：多半是模型名写错，或该模型不支持当前参数（如 tools/stream_options）",
    401: "Key 无效或已作废：确认没复制多余空格、没粘贴到已删除的 Key",
    402: "余额不足：账户欠费或免费额度已用尽，去控制台充值/看用量",
    403: "无权限：Key 未实名、未开通该模型，或被 IP 白名单挡了",
    404: "地址或模型不存在：检查 LLM_API_BASE（智谱是 .../api/paas/v4，别再手动加 /v1）与模型名",
    429: "限流或额度耗尽：并发太高或当日配额用完，稍后重试/提额",
    500: "上游故障：模型服务端异常，稍后重试，持续则换模型",
    502: "网关错误：base 配错或中间代理挂了",
    503: "服务不可用：上游维护中",
}


def mask(key: str) -> str:
    """Key 脱敏：只留前 6 位与长度。审计/截图/贴群里都不会泄漏。"""
    if not key:
        return "未配置"
    return f"{key[:6]}…（共 {len(key)} 位）"


def explain(exc: Exception) -> str:
    """把底层异常翻译成一句能直接动手的话。"""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        hint = STATUS_HINTS.get(code, "未知状态码")
        # 上游一般会在 body 里给一句原因，截断后附上，避免只能看到 401 三个字
        body = (exc.response.text or "").strip().replace("\n", " ")[:200]
        return f"HTTP {code} — {hint}\n         上游原文：{body}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时：网络不通或 LLM_TIMEOUT 太小（大模型首包慢，建议 ≥60s）"
    if isinstance(exc, httpx.ConnectError):
        return "连不上：检查 LLM_API_BASE 域名、代理与防火墙"
    return f"{type(exc).__name__}: {exc}"


def print_config(s) -> None:
    print("── 1. 配置 ─────────────────────────────────────")
    print(f"  对话    provider={s.llm_provider}  model={s.llm_chat_model}")
    print(f"          base={s.llm_api_base or '(默认)'}")
    print(f"  向量    provider={s.embed_provider}  model={s.embed_model}  dim={s.embed_dim}")
    print(f"  Key     {mask(s.llm_api_key)}")
    if s.llm_provider == "mock" or s.embed_provider == "mock":
        print(f"  {WARN} 当前有环节跑在 mock 上：链路能通，但回答是回显式的、向量没有语义。")
        print("         要验真实 Key，请在 .env 里把 LLM_PROVIDER / EMBED_PROVIDER 设为 openai。")


async def check_chat(s, verbose: bool) -> bool:
    print("\n── 2. 对话连通性 ───────────────────────────────")
    if (s.llm_provider or "mock").lower() == "mock":
        print(f"  {WARN} provider=mock，跳过（这不是失败）")
        return True
    if not s.llm_api_key:
        print(f"  {FAIL} LLM_API_KEY 为空，无法调用")
        return False

    llm = get_llm()
    try:
        # 用最短的请求探活：只要模型回话，就说明 Key/网络/模型名三者都对
        res = await llm.chat([{"role": "user", "content": "回复两个字：在线"}], temperature=0)
    except Exception as exc:  # noqa: BLE001 — 这里就是要兜住一切并翻译成人话
        print(f"  {FAIL} {explain(exc)}")
        return False

    print(f"  {OK} 通  延迟 {res.latency_ms} ms  返回模型={res.model}")
    if res.usage:
        print(f"         usage={res.usage}")
    if verbose:
        print(f"         答复原文：{(res.text or '').strip()[:100]}")
    return True


async def check_embedding(s, verbose: bool) -> bool:
    print("\n── 3. 向量连通性（维度必须等于 EMBED_DIM）───────")
    client = EmbeddingClient()
    if client.provider == "mock":
        print(f"  {WARN} EMBED_PROVIDER=mock，跳过（向量无语义，只验证链路）")
    if not s.llm_api_key and client.provider == "openai":
        print(f"  {FAIL} LLM_API_KEY 为空，无法调用真实 embedding")
        return False

    try:
        vec = await client.embed_query("术后随访制度")
    except Exception as exc:  # noqa: BLE001
        print(f"  {FAIL} {explain(exc)}")
        return False

    dim = int(vec.shape[0])
    if dim != s.embed_dim:
        # 不该发生（客户端有守卫），留着是双保险：维度错了检索会静默引错条文
        print(f"  {FAIL} 返回 {dim} 维 ≠ 配置 EMBED_DIM={s.embed_dim}")
        return False
    print(f"  {OK} 通  维度 {dim}（与 EMBED_DIM 一致）")
    if verbose:
        print(f"         前 4 维：{[round(float(x), 4) for x in vec[:4]]}")
    if (s.llm_provider or "mock").lower() == "mock":
        print("         提示：对话还在 mock、向量已真实——这种混搭只用来单独验 Key，别拿来演示")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="模型 Key 连通性自检")
    parser.add_argument("--verbose", action="store_true", help="打印答复原文 / usage / 向量前几维")
    args = parser.parse_args()

    s = get_settings()
    print_config(s)

    results = [
        await check_chat(s, args.verbose),
        await check_embedding(s, args.verbose),
    ]

    print("\n── 结论 ────────────────────────────────────────")
    if all(results):
        print(f"  {OK} 配置可用。上线前记得：换过 embedding 必须跑 reindex_embeddings，")
        print("         且 Key 只留在 .env（已 gitignore），永远不要写进代码、文档或提交记录。")
        return 0
    print(f"  {FAIL} 有项目未通过，按上面每一行的提示处理后重跑本脚本。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
