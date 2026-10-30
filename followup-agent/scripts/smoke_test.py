# -*- coding: utf-8 -*-
"""D3–D4 冒烟测试：一条命令验证「上传→发布→检索→问答→溯源」整条链路。

用法（服务需已启动）：
    python scripts/smoke_test.py --base-url http://127.0.0.1:8000

它会：
  1. 注册一个随机管理员/医护账号（避免与已有数据冲突）
  2. 上传示例制度文档（draft）→ 提问（应拒答，因为草稿不进检索）
  3. 发布 → 再提问（应带引用回答）
  4. 校验：SSE 流式事件完整、message.meta 里有 citations、会话可回溯
退出码 0 表示全部通过，非 0 表示有断言失败——可直接挂到 CI。
"""
from __future__ import annotations

import argparse
import json
import random
import string
import sys
from pathlib import Path

import httpx

SAMPLE_DOC = """# 出院后随访管理制度（试行）

## 一、随访范围与频次
本院出院患者随访范围包括术后患者、慢病出院患者。
骨科术后随访节点为出院后第 3 天、第 7 天、第 30 天，共三次。

## 二、红旗症状与升级
随访过程中发现以下红旗症状须立即停止问卷并升级：体温超过 38.5 摄氏度、
切口红肿渗液、剧烈疼痛不能缓解、胸闷气促、意识改变。
处理流程：立即终止问卷，2 小时内电话通知主管医生并记录接手人姓名与时间，
24 小时内随访专员回访确认处理结果。红旗症状记录不允许通过微信群或个人表格传输。

## 三、完成率口径
随访完成率 = 规定时间内完成有效随访的人次 / 应随访人次。
有效随访指电话接通且完成问卷，或短信确认回复。

## 四、本条用于自动化校验
本制度的内部校验编号为 {marker}，对应出院后第 5 天加做一次电话复核。
"""



def check(condition: bool, message: str) -> bool:
    print(("  ✅ " if condition else "  ❌ ") + message)
    return condition


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

    ok = True
    with httpx.Client(base_url=base, timeout=120) as c:
        print("[1] 健康检查")
        health = c.get("/health").json()
        ok &= check(health.get("status") == "ok", f"health={health}")

        print("[2] 注册并登录")
        for username, role in ((f"smoke_admin_{suffix}", "admin"), (f"smoke_nurse_{suffix}", "staff")):
            c.post(
                "/api/v1/auth/register",
                json={"username": username, "password": "Passw0rd!", "full_name": username, "role": role},
            )
        admin = c.post(
            "/api/v1/auth/login",
            json={"username": f"smoke_admin_{suffix}", "password": "Passw0rd!"},
        ).json()["access_token"]
        staff = c.post(
            "/api/v1/auth/login",
            json={"username": f"smoke_nurse_{suffix}", "password": "Passw0rd!"},
        ).json()["access_token"]
        ok &= check(bool(admin and staff), "管理员与医护 token 已获取")

        print("[3] 上传制度文档（应进入 draft）")
        title = f"冒烟测试制度_{suffix}"
        marker = f"SMOKE-{suffix.upper()}"
        resp = c.post(
            "/api/v1/kb/documents",
            headers={"Authorization": f"Bearer {admin}"},
            files={
                "file": (
                    "smoke.md",
                    SAMPLE_DOC.format(marker=marker).encode("utf-8"),
                    "text/markdown",
                )
            },
            data={"title": title, "doc_type": "随访方案"},
        )
        doc = resp.json()
        ok &= check(resp.status_code == 201 and doc.get("status") == "draft", f"上传结果={doc}")

        # 用唯一编号提问：能精确判断命中的是不是「这份新文档」
        question = f"内部校验编号 {marker} 对应哪次随访？"

        print("[4] 发布前提问（这份草稿不应进检索）")
        before = c.post(
            "/api/v1/chat/ask",
            headers={"Authorization": f"Bearer {staff}"},
            json={"question": question},
        ).json()
        ok &= check(
            all(cite["document_id"] != doc["id"] for cite in before["citations"]),
            f"发布前未命中草稿文档，引用={[(x['n'], x['document_id']) for x in before['citations']]}",
        )

        print("[5] 发布文档")
        pub = c.post(
            f"/api/v1/kb/documents/{doc['id']}/publish",
            headers={"Authorization": f"Bearer {admin}"},
        ).json()
        ok &= check(pub.get("status") == "published", f"发布结果={pub}")

        print("[6] 发布后提问（应命中并引用这份新文档）")
        after = c.post(
            "/api/v1/chat/ask",
            headers={"Authorization": f"Bearer {staff}"},
            json={"question": question, "session_id": before["session_id"]},
        ).json()
        ok &= check(
            any(cite["document_id"] == doc["id"] for cite in after["citations"]),
            f"引用={[(x['n'], x['document_id']) for x in after['citations']]}",
        )
        ok &= check(len(after["answer"]) > 5, f"回答前 60 字：{after['answer'][:60]}")

        print("[7] SSE 流式（应有 session/citations/delta/done）")
        events: list[str] = []
        text = ""
        with c.stream(
            "POST",
            "/api/v1/chat",
            headers={"Authorization": f"Bearer {staff}", "Accept": "text/event-stream"},
            json={"question": "患者发烧到39度该怎么处理？", "session_id": before["session_id"]},
        ) as r:
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                events.append(data["type"])
                if data["type"] == "delta":
                    text += data["text"]
                if data["type"] == "replace":
                    text = data["text"]
        ok &= check({"session", "citations", "delta", "done"} <= set(events), f"事件序列={events}")
        ok &= check(len(text) > 5, f"流式拼出的回答前 60 字：{text[:60]}")

        print("[8] 溯源 meta（citations / prompt_version 必须落库）")
        msgs = c.get(
            f"/api/v1/chat/sessions/{before['session_id']}/messages",
            headers={"Authorization": f"Bearer {staff}"},
        ).json()
        assistant = [m for m in msgs if m["role"] == "assistant" and m["meta"]]
        meta = assistant[-1]["meta"] if assistant else {}
        ok &= check(bool(meta.get("prompt_version")), f"prompt_version={meta.get('prompt_version')}")
        ok &= check("citations" in meta, f"citations={meta.get('citations')}")

    print("\n" + ("全部通过 ✅" if ok else "存在失败项 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
