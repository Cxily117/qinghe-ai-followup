# -*- coding: utf-8 -*-
"""三条典型轨迹的端到端验证（阶段 4 · P2/P3）。

对应文档：`docs/agent-trajectories.md`
  A. 只查规范      「心梗患者出院 30 天随访要问哪些内容」
  B. 新建随访      「帮我新建这位冠心病出院患者的随访记录」
  C. 红旗症状      「患者说持续胸痛，需要怎么处理」

它验证的不是"回答好不好听"，而是**过程对不对**：
  - 工具序列是否与预期一致（A 不许碰业务工具，B 必须走校验闸门，C 必须先评估风险）；
  - 每轮是否都落了决策日志（decision / 参数 / 耗时 / 结果摘要）；
  - 写操作是否真的被挂起，确认前库里没有草稿，确认后才出现且状态是 draft。

用法（服务需已启动）：
    python scripts/trajectory_test.py --base-url http://127.0.0.1:8000
退出码 0 = 三条轨迹全部符合预期。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

# 轨迹 B/C 需要"制度依据"，否则规则引擎会诚实地返回 NO_POLICY_EVIDENCE。
# 这是一份最小可用的演示制度，字段与真实上传的 md 一致。
CARDIO_POLICY = """# 冠心病/心梗出院随访制度（演示）

## 一、随访节点
冠心病 PCI 术后患者：出院后第 30 天门诊复查（用药依从性与症状评估），
第 90 天电话随访（胸痛、气促症状复核），第 180 天门诊复查（心功能与危险因素）。

## 二、红旗症状与处置
出现持续胸痛、胸闷、气促、呼吸困难、大汗、意识改变、晕厥时，
立即停止随访问卷，10 分钟内电话联系主管医生或胸痛中心，必要时拨打急救电话；
不得让患者自行前往医院。发热超过 38.5℃、切口渗液红肿按感染流程处理，
2 小时内通知主管医生并记录接手人。

## 三、留痕要求
红旗症状处置必须在系统内记录处理人与时间，不得通过个人微信传输患者信息。
"""


def check(ok: bool, msg: str) -> bool:
    print(("  ✅ " if ok else "  ❌ ") + msg)
    return ok


def sse_post(c: httpx.Client, path: str, token: str, body: dict) -> list[dict]:
    """跑一次 SSE，返回事件列表（每项含 type 与原始 data）。"""
    events: list[dict] = []
    with c.stream(
        "POST",
        path,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        json=body,
    ) as r:
        cur: dict = {}
        for line in r.iter_lines():
            if line.startswith("event:"):
                cur["event"] = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    cur["data"] = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    cur["data"] = {}
                events.append(dict(cur))
                cur = {}
    return events


def tools_of(events: list[dict]) -> list[str]:
    """从 step(start) 事件里取出工具序列（每个工具只记一次）。"""
    seq: list[str] = []
    for e in events:
        if e.get("event") == "step" and (e.get("data") or {}).get("status") == "start":
            seq.append(e["data"]["tool"])
    return seq


def done_of(events: list[dict]) -> dict:
    for e in events:
        if e.get("event") == "done":
            return e.get("data") or {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin", default="demo_admin")
    ap.add_argument("--nurse", default="demo_nurse")
    ap.add_argument("--password", default="Passw0rd!")
    ap.add_argument(
        "--force",
        action="store_true",
        help="即使接了真实模型也照跑（真实模型的工具序列不保证与预期完全一致）",
    )
    args = ap.parse_args()

    # 这个脚本断言的是「mock 脚本化路径」的固定序列，属于回归测试。
    # 接上真实模型后模型可能合理走别的路径（比如多检索一次），断言就不再有意义，
    # 此时自动跳过并说明原因，而不是把正常行为报成失败。
    try:
        # 脚本以 scripts/ 为工作目录运行，sys.path 里没有项目根 → 手动补上，
        # 否则 import 失败会被误判成 mock，导致该跳过时没跳过
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.core.config import get_settings

        provider = get_settings().llm_provider
    except Exception as exc:  # 读不到配置就按 mock 处理（老环境仍可跑）
        print(f"（无法读取配置，按 mock 处理：{exc}）")
        provider = "mock"
    if provider != "mock" and not args.force:
        print(
            f"跳过：当前 LLM_PROVIDER={provider}（真实模型），"
            "固定工具序列断言仅对 mock 有效。如需强制运行请加 --force。"
        )
        return 0

    ok = True
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120) as c:
        admin = c.post(
            "/api/v1/auth/login", json={"username": args.admin, "password": args.password}
        ).json()["access_token"]
        nurse = c.post(
            "/api/v1/auth/login", json={"username": args.nurse, "password": args.password}
        ).json()["access_token"]
        ah = {"Authorization": f"Bearer {admin}"}

        # ---- 0. 准备制度（幂等：同名文档会被覆盖成新版本） ----
        print("[0] 准备演示制度")
        up = c.post(
            "/api/v1/kb/documents",
            headers=ah,
            files={"file": ("cardio.md", CARDIO_POLICY.encode(), "text/markdown")},
            data={"title": "冠心病出院随访制度（演示）", "doc_type": "随访方案"},
        )
        doc = up.json()
        c.post(f"/api/v1/kb/documents/{doc['id']}/publish", headers=ah)
        ok &= check(doc.get("status") in ("draft", "published"), f"制度已入库 id={doc['id']}")

        # ================================================================
        # 轨迹 A：只查规范，不调用业务工具
        # ================================================================
        print("\n[A] 「心梗患者出院 30 天随访要问哪些内容」")
        sid_a = c.post("/api/v1/chat/sessions", headers={"Authorization": f"Bearer {nurse}"}, json={}).json()["id"]
        ev_a = sse_post(
            c, "/api/v1/agent/chat", nurse,
            {"session_id": sid_a, "question": "心梗患者出院 30 天随访要问哪些内容？"},
        )
        seq_a = tools_of(ev_a)
        ok &= check(seq_a == ["search_policy"], f"工具序列={seq_a}（应为 ['search_policy']）")
        ok &= check("resolve_patient" not in seq_a, "未调用 resolve_patient（无患者上下文，不强行解析）")
        ok &= check(
            not any(t in seq_a for t in ("check_followup_rule", "create_followup_task_draft")),
            "未触发任何写操作或合规校验",
        )
        done_a = done_of(ev_a)
        ok &= check(bool(done_a.get("citations")), f"带引用回答：{len(done_a.get('citations') or [])} 条")
        ok &= check(done_a.get("status") == "succeeded", f"run 状态={done_a.get('status')}")

        # ================================================================
        # 轨迹 B：新建随访（解析 → 检索 → 校验 → 生成草稿 → 等确认 → 保存）
        # ================================================================
        print("\n[B] 「帮我新建这位冠心病出院患者的随访记录」")
        sid_b = c.post("/api/v1/chat/sessions", headers={"Authorization": f"Bearer {nurse}"}, json={}).json()["id"]
        ev_b = sse_post(
            c, "/api/v1/agent/chat", nurse,
            {"session_id": sid_b, "question": "帮我新建这位冠心病出院患者（住院号 ZY2026004）的随访记录"},
        )
        seq_b = tools_of(ev_b)
        expect_b = ["resolve_patient", "search_policy", "check_followup_rule", "create_followup_task_draft"]
        ok &= check(seq_b == expect_b, f"工具序列={seq_b}\n       预期={expect_b}")
        ok &= check(
            seq_b.index("check_followup_rule") < seq_b.index("create_followup_task_draft")
            if "create_followup_task_draft" in seq_b else False,
            "创建草稿排在合规校验之后（顺序约束生效）",
        )
        confirm_ev = next((e for e in ev_b if e.get("event") == "confirm_required"), None)
        ok &= check(confirm_ev is not None, "收到 confirm_required（写操作已挂起等人工确认）")
        done_b = done_of(ev_b)
        ok &= check(done_b.get("status") == "pending_confirm", f"run 状态={done_b.get('status')}")
        ok &= check(bool(done_b.get("awaiting_confirm")), "done.awaiting_confirm=true")
        run_uid = done_b.get("run_uid", "")

        # 确认前：库里不该**新增**草稿（用 id 集合判断，避免被历史数据干扰）
        def draft_ids() -> set[int]:
            rows = c.get(
                "/api/v1/tasks/drafts", headers={"Authorization": f"Bearer {nurse}"}
            ).json()
            rows = rows if isinstance(rows, list) else (rows.get("items") or [])
            return {d["id"] for d in rows}

        ids_before = draft_ids()
        ok &= check(True, f"运行前草稿 {len(ids_before)} 张（记录基线）")

        # 回放：逐轮日志
        replay = c.get(f"/api/v1/agent/runs/{run_uid}", headers={"Authorization": f"Bearer {nurse}"}).json()
        rounds = replay.get("rounds") or []
        ok &= check(len(rounds) >= 4, f"逐轮决策日志 {len(rounds)} 条")
        ok &= check(
            all(r.get("decision") in ("tool_call", "final", "blocked", "confirm") for r in rounds),
            "每条日志都有 decision 字段",
        )
        ok &= check(
            all(r.get("latency_ms") is not None and r.get("result_digest") for r in rounds),
            "每条日志都有耗时与结果摘要",
        )
        rule_round = next((r for r in rounds if r["tool"] == "check_followup_rule"), {})
        ok &= check(bool((rule_round.get("result_digest") or "").find("passed") >= 0), "校验轮的结果摘要含 passed")
        create_round = next((r for r in rounds if r["tool"] == "create_followup_task_draft"), {})
        ok &= check(create_round.get("decision") == "confirm", f"创建轮 decision={create_round.get('decision')}")

        # 人工确认 → 落库
        conf_id = (confirm_ev.get("data") or {}).get("confirmation_id") if confirm_ev else None
        conf = c.post(
            f"/api/v1/agent/runs/{run_uid}/confirm",
            headers={"Authorization": f"Bearer {nurse}"},
            json={"action": "confirm", "note": "轨迹测试：核对节点后保存"},
        )
        ok &= check(conf.status_code == 200, f"确认接口 HTTP {conf.status_code}")
        conf_body = conf.json()
        draft_id = conf_body.get("draft_id")
        ok &= check(bool(draft_id), f"落库草稿 id={draft_id}")

        draft = c.get(f"/api/v1/tasks/{draft_id}", headers={"Authorization": f"Bearer {nurse}"}).json()
        ids_after = draft_ids()
        ok &= check(
            draft_id not in ids_before,
            "确认前该草稿不在库里（写操作确实被闸门挂起，不是先建后改状态）",
        )
        ok &= check(
            ids_after - ids_before == {draft_id},
            f"确认后新增且仅新增这一张：{sorted(ids_after - ids_before)}",
        )
        ok &= check(draft.get("status") == "draft", f"草稿状态={draft.get('status')}（AI 只能到 draft）")
        offsets = [i["offset_days"] for i in (draft.get("content") or {}).get("items", [])]
        ok &= check(offsets == [30, 90, 180], f"节点={offsets}（应来自制度 30/90/180）")
        ok &= check(
            ((draft.get("content") or {}).get("ai") or {}).get("confirmed_via") == "agent_confirm_card",
            "草稿留痕：confirmed_via=agent_confirm_card",
        )

        # 重复确认 → 409（一次性）
        again = c.post(
            f"/api/v1/agent/runs/{run_uid}/confirm",
            headers={"Authorization": f"Bearer {nurse}"},
            json={"action": "confirm"},
        )
        ok &= check(again.status_code == 409, f"重复确认 HTTP {again.status_code}（应为 409）")

        # 越权确认 → 404
        other = c.post(
            "/api/v1/auth/register",
            json={"username": "traj_other", "password": args.password, "full_name": "他人", "role": "staff"},
        )
        other_tok = c.post(
            "/api/v1/auth/login", json={"username": "traj_other", "password": args.password}
        ).json().get("access_token", "")
        if other_tok:
            denied = c.post(
                f"/api/v1/agent/runs/{run_uid}/confirm",
                headers={"Authorization": f"Bearer {other_tok}"},
                json={"action": "confirm"},
            )
            ok &= check(denied.status_code in (404, 409), f"他人确认 HTTP {denied.status_code}")

        # ================================================================
        # 轨迹 C：红旗症状（评估 → 制度提示，不排普通随访）
        # ================================================================
        print("\n[C] 「患者说持续胸痛，需要怎么处理」")
        sid_c = c.post("/api/v1/chat/sessions", headers={"Authorization": f"Bearer {nurse}"}, json={}).json()["id"]
        ev_c = sse_post(
            c, "/api/v1/agent/chat", nurse,
            {"session_id": sid_c, "question": "患者说持续胸痛，需要怎么处理？"},
        )
        seq_c = tools_of(ev_c)
        ok &= check(seq_c[:2] == ["assess_red_flag", "search_policy"], f"工具序列={seq_c}")
        ok &= check("create_followup_task_draft" not in seq_c, "红旗场景不生成普通随访草稿")
        done_c = done_of(ev_c)
        flags_c = done_c.get("flags") or {}
        ok &= check(bool(flags_c.get("red_flag_high")), f"flags 已标记 red_flag_high={flags_c.get('red_flag_high')}")
        answer_c = done_c.get("final_text") or ""
        ok &= check(("主管医生" in answer_c) or ("急救" in answer_c), f"回答含处置动作：{answer_c[:60]}…")
        run_c = c.get(f"/api/v1/agent/runs/{done_c.get('run_uid','')}", headers={"Authorization": f"Bearer {nurse}"}).json()
        ok &= check(bool((run_c.get("route") or {}).get("risk_ticket")), "已签发 risk_ticket（可升级）")

        # 三条轨迹的日志汇总，便于人工扫描
        print("\n[审计] 轨迹 B 的逐轮日志（工具 / 耗时 / 摘要）")
        for r in rounds:
            print(
                f"  轮{r['round']:>2} {r['decision']:<9} {r['tool']:<26} "
                f"{r['latency_ms']:>4}ms  {(r.get('result_digest') or '')[:56]}"
            )

    print("\n" + ("三条轨迹全部符合预期 ✅" if ok else "存在不符合预期的项 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
