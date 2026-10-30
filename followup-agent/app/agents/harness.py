"""Agent Harness：有界自主循环的骨架（阶段 4 的核心引擎）。

## 为什么是「自建 Harness」而不是 LangGraph / AutoGen / Dify

结论先说：这一个流程用框架是负收益。四个具体理由：

1. **闸门必须在代码里，不能是图上的一个节点。**
   我们要的"没校验就不给创建工具"是一种**能力裁剪**（schema 不下发）+ **票据校验**，
   不是在流程图上连一条边。LangGraph 的边可以绕过（模型照样能调工具节点），
   最终还得在工具内部再写一遍校验——真正起作用的仍是这层代码，框架只是多了一层抽象。
2. **审计字段是我们自定义的。** `args_redacted / result_digest / ticket_id / decision / model_note`
   是为"能向医务处举证"设计的，框架的 trace 格式（span/event）要落成这些字段仍需大量映射代码。
3. **依赖与可解释性。** 院内环境要过等保、要能离线部署、要能向评审解释每一行。
   自建 Harness 总共 200 行、零新依赖；引入框架要多 20+ 个包和一套概念。
4. **降级路径。** `strict` 模式要求"只读工具 + 固定流程"，这在本文件里就是一个参数；
   在框架里要重画一张图。

代价也要写清楚：没有框架就没有现成的 checkpoint/断点续跑、并行工具调用、可视化 trace。
本流程节点少（≤5 轮）、串行、单写操作，这些能力用不上。
将来若要做多智能体协作（例如"随访排程 agent + 质控 agent"），再换框架也不迟——
`dispatch` 与 `RoundRecord` 这两个契约是稳定的，替换的是执行器不是业务。

## 循环语义（一轮 = 一次模型决策）

    模型决策 ──► 有 tool_calls ──► 校验工具可见性 ──► 执行 ──► 结果回填 ──┐
        ▲                                                                 │
        └─────────────────────────────────────────────────────────────────┘
                    └─ 无 tool_calls ──► 最终答复（结束）
                    └─ 写操作返回 pending_confirm ──► 挂起等人工确认（结束）

- **最多 5 轮**（`agent_max_steps`），第 5 轮仍要调工具就直接停：宁可转人工，不无限烧钱。
- 每轮都产出一条 `RoundRecord`，由调用方落 `agent_tool_calls`。
- 一轮只执行一个工具调用：顺序约束要求"一步一验证"，并发执行会打乱因果链。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from app.agents.tools import available_specs, enabled_for_mode
from app.agents.runtime import RunState

# 院内标识：ZY2026001 / P123456 / A0000123
_ID_RE = re.compile(r"\b[A-Za-z]{1,3}\d{5,}\b")

# 审计表里这些键必须打码（患者标识/症状原文属于 PHI）
_MASKED_KEYS = {"patient_ref", "patient_id", "symptoms", "name"}
# 这些键保留原文（否则无法复盘"它到底查了什么"），但仍先过 ID 掩码
_KEPT_KEYS = {"query", "reason", "plan_type"}


def mask_ids(text: str) -> str:
    """把文本里的院内编号打码：保留前两位，便于人工比对又不可反查。"""
    return _ID_RE.sub(lambda m: m.group(0)[:2] + "*" * (len(m.group(0)) - 2), text or "")


def redact_arg_value(value: str) -> str:
    """单个值打码：保留首尾各 1 位。"""
    if not value:
        return ""
    if len(value) <= 2:
        return "*" * len(value)
    return value[:1] + "*" * (len(value) - 2) + value[-1:]


def redact_args(args: dict | None) -> dict:
    """审计入库前的入参脱敏。

    注意 query 也要过 `mask_ids`：模型会把医护的整句原话（含住院号）
    当作检索词回传，只按键名脱敏会漏掉这种情况。
    """
    out: dict = {}
    for k, v in (args or {}).items():
        if isinstance(v, list):
            out[k] = f"[{len(v)} items]"
        elif isinstance(v, str):
            if k in _MASKED_KEYS:
                out[k] = redact_arg_value(v)
            elif k in _KEPT_KEYS:
                out[k] = mask_ids(v)[:60]
            else:
                out[k] = mask_ids(v)[:40]
        else:
            out[k] = v
    return out


def result_digest(payload: dict) -> str:
    """结果摘要：脱敏后截断。全文不入库（工具结果常是整段制度条文）。"""
    return mask_ids(json.dumps(payload, ensure_ascii=False))[:250]


@dataclass
class RoundRecord:
    """一轮决策的完整记录 —— 这就是「日志表」一行的内存形态。

    decision 取值：
      tool_call  正常调用工具
      blocked    模型调用了当前未下发的工具（企图跳步的硬证据）
      final      本轮不调工具、给出答复
      confirm    写操作被闸门拦下，产出待确认单
    """

    round_no: int
    decision: str
    tool: str = ""
    args_redacted: dict = field(default_factory=dict)
    ok: bool = True
    error_code: str = ""
    result_digest: str = ""
    ticket_id: str = ""
    latency_ms: int = 0
    model_note: str = ""
    # 只在内存里传给调用方，不落库（可能含条文本与日期）
    payload: dict | None = None


@dataclass
class HarnessOutcome:
    status: str = "succeeded"  # succeeded|max_steps|pending_confirm|failed|handoff
    final_text: str = ""
    rounds: list[RoundRecord] = field(default_factory=list)
    pending: dict | None = None  # confirm_required 的完整内容（含归一化草稿）


# dispatch：工具名 + 入参 → (结果, 耗时ms)。由 service 注入，便于替换实现与测试。
Dispatch = Callable[[str, dict], Awaitable[tuple[dict, int]]]

MAX_STEPS_TEXT = (
    "这轮处理步骤过多（已用尽 5 轮），已停止自动处理并转交人工。"
    "为避免出错，请人工确认随访安排后再录入。"
)


class AgentHarness:
    """一次运行的执行器。无状态依赖外部数据库：持久化由调用方在 round 事件里做。"""

    def __init__(
        self,
        *,
        llm,
        state: RunState,
        messages: list[dict],
        dispatch: Dispatch,
        max_rounds: int = 5,
        mode: str = "bounded",
        tool_result_chars: int = 600,
        model_name: str = "",
    ):
        self.llm = llm
        self.state = state
        self.messages = messages
        self.dispatch = dispatch
        self.max_rounds = max(1, int(max_rounds))
        self.mode = mode
        self.tool_result_chars = tool_result_chars
        self.model_name = model_name
        self.outcome = HarnessOutcome()

    # ------------------------------------------------------------ 主循环

    async def run(self) -> AsyncIterator[dict]:
        """驱动循环，逐事件产出。调用方消费完生成器后读 `self.outcome`。"""
        enabled = enabled_for_mode(self.mode)

        for round_no in range(1, self.max_rounds + 1):
            specs = available_specs(self.state, enabled=enabled)
            if not specs:
                # 只剩兜底也可能被 unlock 掉，这种情况直接按失败收口，不空转
                self.outcome.status = "failed"
                self.outcome.final_text = "当前没有可用工具，已转人工处理。"
                return

            # ---- ① 模型决策 ----
            result = await self.llm.chat_tools(
                self.messages, [sp.schema for sp in specs]
            )
            self.model_name = result.model or self.model_name
            note = (result.text or "").strip()
            calls = (result.extra or {}).get("tool_calls") or []

            if not calls:
                # 不调工具 = 给出最终答复，循环自然结束
                record = RoundRecord(
                    round_no=round_no,
                    decision="final",
                    ok=True,
                    model_note=_clip(note, 300),
                    result_digest=mask_ids(note)[:120],
                )
                self.outcome.rounds.append(record)
                yield {"type": "round", "record": record}
                self.outcome.status = "handoff" if "handoff" in self.state.flags else "succeeded"
                self.outcome.final_text = note
                return

            # 一轮只执行一个：顺序约束要求"一步一验证"
            call = calls[0]
            spec = next((sp for sp in specs if sp.name == call.name), None)

            # ---- ② 可见性校验（调了未下发的工具 = 企图跳步）----
            if spec is None:
                payload = {
                    "ok": False,
                    "error": "tool_unavailable",
                    "message": (
                        f"工具 {call.name} 当前不可用：前置校验未完成或该能力未开放。"
                        "请按标准顺序先完成校验；若无法完成，请调用 handoff_to_human。"
                    ),
                }
                self._append_tool_exchange(call, payload, round_no)
                self.state.flags.add("blocked_tool_call")
                record = RoundRecord(
                    round_no=round_no,
                    decision="blocked",
                    tool=call.name,
                    args_redacted=redact_args(call.arguments),
                    ok=False,
                    error_code="TOOL_UNAVAILABLE",
                    result_digest=result_digest(payload),
                    model_note=_clip(note, 300),
                    payload=payload,
                )
                self.outcome.rounds.append(record)
                yield {"type": "round", "record": record}
                yield {
                    "type": "step",
                    "seq": round_no,
                    "tool": call.name,
                    "status": "blocked",
                    "label": "已拦截越权调用",
                    "detail": {"reason": "tool_unavailable"},
                }
                continue

            # ---- ③ 执行工具 ----
            yield {
                "type": "step",
                "seq": round_no,
                "tool": call.name,
                "status": "start",
                "label": f"正在{spec.label or call.name}…",
            }
            started = time.perf_counter()
            try:
                payload, _ = await self.dispatch(call.name, call.arguments or {})
            except Exception as exc:  # 工具内部异常不该整轮崩掉：回灌错误让模型换路
                payload = {"ok": False, "error": "tool_failed", "message": str(exc)[:200]}
            cost_ms = int((time.perf_counter() - started) * 1000)
            self.state.touch(call.name)

            # ---- ④ 结果回填（模型下一轮据此再决策）----
            self._append_tool_exchange(call, payload, round_no)

            is_pending = bool(payload.get("pending_confirm"))
            record = RoundRecord(
                round_no=round_no,
                decision="confirm" if is_pending else "tool_call",
                tool=call.name,
                args_redacted=redact_args(call.arguments),
                ok=payload.get("ok", True) is not False,
                error_code=str(payload.get("error") or ""),
                result_digest=result_digest(payload),
                ticket_id="",  # 票据指纹由 service 补（它知道票据原文）
                latency_ms=cost_ms,
                model_note=_clip(note, 300),
                payload=payload,
            )
            self.outcome.rounds.append(record)
            yield {"type": "round", "record": record}

            if is_pending:
                # ★ 强制中断：不再让模型继续（它没有能力"确认"自己的草稿）
                self.outcome.status = "pending_confirm"
                self.outcome.pending = payload
                self.outcome.final_text = str(payload.get("message") or "草稿待确认")
                yield {
                    "type": "confirm_required",
                    "draft": payload.get("draft") or {},
                    "rule_sha": payload.get("rule_sha") or "",
                }
                return

            yield {
                "type": "step",
                "seq": round_no,
                "tool": call.name,
                "status": "ok" if record.ok else "error",
                "label": f"{spec.label or call.name}完成",
                "detail": _step_detail(call.name, payload),
            }
        else:
            # for-else：5 轮用尽仍在调工具
            self.outcome.status = "max_steps"
            self.outcome.final_text = MAX_STEPS_TEXT
            self.state.flags.add("max_steps")

    # ------------------------------------------------------------ 内部

    def _append_tool_exchange(self, call, payload: dict, round_no: int) -> None:
        """按 OpenAI 兼容格式把「模型要调工具」和「工具结果」写回消息列表。"""
        self.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.call_id or f"call_{round_no}",
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.raw_arguments or "{}",
                        },
                    }
                ],
            }
        )
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.call_id or f"call_{round_no}",
                "content": json.dumps(payload, ensure_ascii=False)[
                    : self.tool_result_chars * 4
                ],
            }
        )


def _clip(text: str, limit: int) -> str:
    return mask_ids((text or "").strip())[:limit]


def _step_detail(tool: str, payload: dict) -> dict:
    """给前端时间线的短摘要（不含条文全文）。"""
    if tool == "resolve_patient":
        return {
            "found": payload.get("found", False),
            "patient": payload.get("patient_display") or "",
            "dept": payload.get("dept") or "",
        }
    if tool == "search_policy":
        return {"hits": payload.get("count", 0)}
    if tool == "list_existing_tasks":
        return {"existing": payload.get("count", 0)}
    if tool == "check_followup_rule":
        return {
            "passed": payload.get("passed", False),
            "violations": len(payload.get("violations") or []),
            "profile": payload.get("profile_label") or "",
        }
    if tool == "assess_red_flag":
        return {"level": payload.get("level", ""), "hits": len(payload.get("hits") or [])}
    if tool == "create_followup_task_draft":
        return {
            "pending": bool(payload.get("pending_confirm")),
            "items": len((payload.get("draft") or {}).get("items") or []),
        }
    if tool == "escalate_to_clinician":
        return {"escalated": payload.get("escalated", False), "no": payload.get("escalation_no", "")}
    if tool == "handoff_to_human":
        return {"handoff": True}
    return {}


def digest_of(token: str) -> str:
    """票据原文只进内存，入库只存指纹。"""
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""
