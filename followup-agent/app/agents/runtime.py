"""智能体运行时：工具注册表 + 运行状态 + 校验票据。

这个文件是「顺序约束不靠提示词」的落地点，三件事都在这里：

1. **工具注册表带 meta**（分类 / 解锁条件 / 是否写操作）
   工具不是一次性全塞给模型的。`available_schemas(state)` 按当前运行状态过滤，
   没解锁的工具**根本不出现在 tools 列表里**——模型调用一个不存在的函数，
   比让它"遵守规则"可靠得多（提示词会被诱导、会随模型升级漂移，函数列表不会）。

2. **RunState**：一次请求内的信封，记录已解析的患者、已签发的票据、已用次数。
   它同时是"模型能做什么"的判据和审计用的路线摘要来源。

3. **票据（ticket）**：HMAC 签名的短时凭证。
   签名解决"伪造"，nonce 表解决"重放"，exp 解决"过期"，
   内嵌 patient_id / rule_sha 解决"张冠李戴"和"拿旧版制度建草稿"。
   票据只由服务端在工具返回里给出，模型无法凭空造出一张能验过的票。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.config import get_settings
from app.core.errors import AppError

# 工具分类：读操作放开，判定的 passed 由服务端算，写操作必须持票
READ = "read"
JUDGE = "judge"
WRITE = "write"
HANDOFF = "handoff"


@dataclass(frozen=True)
class ToolSpec:
    """工具元数据：schema 之外，还描述「什么时候能出现在列表里」。"""

    name: str
    schema: dict
    category: str
    # 解锁条件：返回 True 才下发给模型。参数是 RunState。
    unlock: Callable[["RunState"], bool] = lambda state: True
    # 是否需要票据，以及需要哪种（write 类工具由 handler 再验一次，双保险）
    ticket_kind: str = ""
    # 单轮最多调用几次（None = 不单独限制，受 agent_max_steps 总闸约束）
    max_calls: int | None = None
    label: str = ""  # 给前端步骤时间线用的中文名


@dataclass
class RunState:
    """一次智能体运行的状态信封（不落库，随请求生命周期存活）。"""

    run_uid: str
    user_id: int
    session_id: int | None = None
    mode: str = "bounded"
    question: str = ""

    # 已解析的事实（模型只能通过工具填充，不能直接写）
    patient_id: str = ""
    patient_label: str = ""
    plan_type: str = ""
    discharge_date: str = ""

    # 已签发的票据（token 原文，供 handler 校验后放行）
    rule_ticket: str = ""
    risk_ticket: str = ""
    # 规则指纹：票据里嵌的就是它，审计时用来对齐"当时按哪版规则/哪批条文算的"
    rule_sha: str = ""

    # 路线摘要：按顺序记录调过的工具，用于审计与「跳步」检测
    route: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    # 检索登记表：一次运行内跨多次 search_policy 连续编号，
    # 否则模型查两轮后 [1] 指哪条就说不清，引用会串。
    citations: list[dict] = field(default_factory=list)
    # 检索到的条文正文（护栏做「节点外推拦截」时要比对：问题问的时点在不在条文里）。
    # 与 citations 分开存：citations 只有 preview（给前端展示引用卡片），
    # 判定需要更完整的正文，否则 600 字之外的节点会被误判成"条文里没有"。
    retrieved_texts: list[str] = field(default_factory=list)

    def touch(self, tool: str) -> None:
        self.route.append(tool)
        self.counters[tool] = self.counters.get(tool, 0) + 1

    def called(self, tool: str) -> bool:
        return self.counters.get(tool, 0) > 0

    def route_digest(self) -> dict:
        """写进 agent_runs.route 的摘要。"""
        return {
            "route": self.route,
            "tools": dict(self.counters),
            "patient": bool(self.patient_id),
            "rule_ticket": bool(self.rule_ticket),
            "risk_ticket": bool(self.risk_ticket),
            "rule_sha": self.rule_sha,
            "flags": sorted(self.flags),
        }


# ---------------------------------------------------------------- 票据


def _secret() -> bytes:
    s = get_settings()
    raw = s.agent_ticket_secret or f"agent-ticket::{s.jwt_secret}"
    return hashlib.sha256(raw.encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_ticket(
    *,
    kind: str,
    run_uid: str,
    patient_id: str = "",
    plan_type: str = "",
    rule_sha: str = "",
) -> str:
    """签发一张票据：`base64(payload).sig`。

    刻意做成「自包含」而不是「存库拿 id」：
    验签不需要查库，多进程/多副本部署下也成立；库里只留 nonce 防重放。
    """
    s = get_settings()
    payload = {
        "k": kind,
        "n": secrets.token_hex(8),
        "r": run_uid,
        "p": patient_id,
        "t": plan_type,
        "s": rule_sha,
        "exp": int(time.time()) + s.agent_ticket_ttl,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:16])
    return f"{body}.{sig}"


def verify_ticket(
    token: str,
    *,
    kind: str,
    patient_id: str | None = None,
    rule_sha: str | None = None,
) -> dict:
    """验签 + 过期 + 归属校验。任何一项不过都抛 AppError（4xx）。"""
    if not token or "." not in token:
        raise AppError("AGENT_TICKET_INVALID")
    body, _, sig = token.rpartition(".")
    expect = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:16])
    # 定长比较，避免时序侧信道；签名不对就没有继续解析的必要
    if not hmac.compare_digest(sig, expect):
        raise AppError("AGENT_TICKET_INVALID")

    try:
        payload = json.loads(_unb64(body))
    except Exception:  # 签名对但结构坏：同样视为无效票
        raise AppError("AGENT_TICKET_INVALID")

    if payload.get("k") != kind:
        raise AppError("AGENT_TICKET_INVALID", "票据类型不匹配，请重新校验")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AppError("AGENT_TICKET_EXPIRED")
    if patient_id and payload.get("p") != patient_id:
        raise AppError("AGENT_TICKET_MISMATCH", "票据不属于该患者，请重新校验")
    if rule_sha and payload.get("s") != rule_sha:
        raise AppError("RULE_VERSION_STALE")
    return payload


def ticket_digest(token: str) -> str:
    """票据摘要：审计表里只存摘要，不存原文（原文等于一张可重放的通行证）。"""
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""


async def consume_ticket(
    db,
    token: str,
    *,
    kind: str,
    patient_id: str | None = None,
    rule_sha: str | None = None,
) -> dict:
    """验签 + 标记已用（一次性）。写操作入口必须调这个，不能只 verify。

    分两步而不是一步：
      1. verify_ticket 是纯计算（签名/过期/归属），不需要数据库，失败即拒；
      2. nonce 落库靠唯一索引判重——`INSERT` 冲突就说明这张票用过了。
         用数据库约束而不是"先查再写"，是因为并发下先查再写必然有窗口，
         而"同一张票建两张草稿"正是要防的事故。
    """
    from datetime import datetime

    from sqlalchemy.exc import IntegrityError

    from app.models.agent import TicketNonce

    payload = verify_ticket(token, kind=kind, patient_id=patient_id, rule_sha=rule_sha)
    db.add(
        TicketNonce(
            nonce=str(payload.get("n") or ""),
            kind=kind,
            used_at=datetime.now(),
        )
    )
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppError("TICKET_REPLAYED")
    return payload
