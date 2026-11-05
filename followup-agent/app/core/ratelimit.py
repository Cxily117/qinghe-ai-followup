"""应用层限流：进程内滑动窗口，挡住「一个账号把模型额度刷光」和「批量撞库」。

为什么必须有（对应上线清单 C2，唯一的成本型阻断项）：
  /api/v1/chat/ask 与 /api/v1/agent/chat 每次请求都要调一次或多次大模型，
  一个 `while true; do curl ...; done` 就能把一天预算烧完；
  /auth/login 不限流则可以离线慢慢撞库。这两类风险不靠"自觉"防。

设计取舍（写下来是为了别人接手时不误解）：
  - **进程内内存实现**，不引入 Redis：当前是单实例部署，够用。
    多 worker / 多副本时各自计数，实际阈值 ≈ 配置值 × 副本数；
    真要横向扩，把 `_WINDOW` 换成 Redis 的 ZSET / 令牌桶即可，接口不变。
  - **两个维度**：登录用户（令牌里的 sub）优先，IP 兜底。
    用户维度伪造不了，是主防线；IP 维度在网关后取 X-Forwarded-For 第一跳，
    那个值客户端可以编，所以只当兜底、不当唯一依据。
  - **回环地址默认豁免**：本地开发、冒烟脚本、运维 curl 不该被自己的限流挡住。
    豁免判断用"生效 IP"，所以测试里带一个假 XFF 就能绕过豁免验证 429。
  - 只拦"花钱"和"可暴力破解"的路径，静态资源、后台页面一律不拦。
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.security import decode_token

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


@dataclass(frozen=True)
class Limit:
    """一条规则：window 秒内最多 limit 次。"""

    limit: int
    window: int = 60


class SlidingWindow:
    """按 key 记录请求时间戳，超过窗口的老记录自动淘汰。

    为什么要滑动窗口而不是固定窗口：固定窗口在边界处能瞬间打出 2 倍流量
    （59 秒 30 次 + 61 秒 30 次），对按次计费的模型调用没意义。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_gc = time.monotonic()

    def check(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        """返回 (是否放行, 超限时建议等待的秒数)。"""
        now = time.monotonic()
        dq = self._hits[key]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            # 最早那次请求滑出窗口后就能放行，据此算 Retry-After
            return False, max(1, int(window - (now - dq[0])) + 1)
        dq.append(now)
        self._maybe_gc(now)
        return True, 0

    def _maybe_gc(self, now: float) -> None:
        """每 5 分钟清一次空桶，避免长期运行内存只增不减。"""
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        for key in [k for k, v in self._hits.items() if not v]:
            self._hits.pop(key, None)

    def reset(self) -> None:
        self._hits.clear()


_WINDOW = SlidingWindow()


def _client_ip(request: Request) -> str:
    """生效 IP：网关后优先 X-Forwarded-For 第一跳，否则用直连地址。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _user_key(request: Request) -> str | None:
    """从令牌里取用户 id 作为限流维度；取不到（未登录）返回 None。"""
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or request.cookies.get("qh_token") or request.headers.get("x-auth-token") or ""
    if not token:
        return None
    try:
        sub = decode_token(token).get("sub")
        return f"u:{int(sub)}" if sub else None
    except (jwt.PyJWTError, TypeError, ValueError):
        return None


def _rules() -> dict[str, tuple[Limit, Limit | None]]:
    """路径 → (用户级规则, IP 级规则)。用户级为 None 表示该路径只看 IP。"""
    s = get_settings()
    chat = Limit(s.rate_limit_user_per_min)
    agent = Limit(max(5, s.rate_limit_user_per_min // 2))  # 智能体更贵，阈值更紧
    ip = Limit(s.rate_limit_ip_per_min)
    login_ip = Limit(s.rate_limit_login_per_min)
    return {
        "/api/v1/chat/ask": (chat, ip),
        "/api/v1/agent/chat": (agent, ip),
        "/api/v1/auth/login": (None, login_ip),
    }


def install_rate_limit(app: FastAPI) -> None:
    """注册全局限流中间件（在 create_app 里调用一次）。

    返回 429 + 与 errors.py 同构的 envelope，前端拿到的仍是
    {code, message, request_id}，不需要为限流单独写一套提示逻辑。
    """

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        s = get_settings()
        if not s.rate_limit_enabled:
            return await call_next(request)

        rule_pair = _rules().get(request.url.path)
        if rule_pair is None:
            return await call_next(request)

        ip = _client_ip(request)
        if s.rate_limit_allow_loopback and ip in LOOPBACK:
            return await call_next(request)

        user_rule, ip_rule = rule_pair
        retry = 0
        blocked_by = ""

        if user_rule is not None:
            key = _user_key(request)
            if key:
                ok, wait = _WINDOW.check(f"{request.url.path}|{key}", user_rule.limit, user_rule.window)
                if not ok:
                    retry, blocked_by = wait, "user"

        if ip_rule is not None and not blocked_by:
            ok, wait = _WINDOW.check(f"{request.url.path}|ip:{ip}", ip_rule.limit, ip_rule.window)
            if not ok:
                retry, blocked_by = wait, "ip"

        if blocked_by:
            rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry), "X-Request-ID": rid},
                content={
                    "code": "RATE_LIMITED",
                    "message": f"请求过于频繁，请 {retry} 秒后再试",
                    "request_id": rid,
                    "retry_after": retry,
                },
            )

        return await call_next(request)
