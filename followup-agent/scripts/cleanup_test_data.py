# -*- coding: utf-8 -*-
"""清理冒烟/调试产生的测试账号与它们的会话数据（幂等，默认只报告不删）。

为什么需要它：
  冒烟测试每次运行都会注册 smoke_admin_xxxx / smoke_nurse_xxxx 这类账号，
  跑几十次就攒下近百个「公网可达的管理员」。这些账号不是业务数据，是测试垃圾，
  留在库里既污染审计页（一眼看过去全是 smoke_*），也是真实的攻击面。

删除范围（按登录名前缀识别，只认这些前缀，不做模糊匹配）：
  smoke_ / zh_ / edge_ / ui_ / ag_ / dbg_ / traj_ / test_ / pwd_
  以及历史遗留的随机短名：adm_ / nur_ / n2_ / a2_ / n3_ / a3_ / n4_ / a4_

保留：
  demo_admin / demo_nurse / demo_nurse2 / nurse001（演示与人工验证用）
  以及 --keep 追加的登录名

安全护栏：
  1. 默认 dry-run，只打印将要删除的数量，加 --apply 才真删；
  2. 整个删除在一个事务里，失败全部回滚；
  3. 删完后若没有任何可用管理员，整体回滚并报错退出（防止把自己锁在门外）；
  4. 关联数据按外键顺序删（会话 → 消息 → run → 工具调用 → 确认单 → 审计标记 → 草稿）。

用法：
    python scripts/cleanup_test_data.py                    # 先看会删什么
    python scripts/cleanup_test_data.py --apply            # 真删
    python scripts/cleanup_test_data.py --apply --keep zhangwei
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402

TEST_PREFIXES = (
    "smoke_",
    "zh_",
    "edge_",
    "ui_",
    "ag_",
    "dbg_",
    "traj_",
    "test_",
    "pwd_",
    "adm_",
    "nur_",
    "n2_",
    "a2_",
    "n3_",
    "a3_",
    "n4_",
    "a4_",
)
KEEP_DEFAULT = ("demo_admin", "demo_nurse", "demo_nurse2", "nurse001")

EMPTY_SQL = (
    "SELECT COUNT(*) FROM sessions s WHERE NOT EXISTS "
    "(SELECT 1 FROM messages m WHERE m.session_id = s.id)"
)


async def prune_empty_sessions(db, apply: bool) -> None:
    """清掉「一条消息都没有」的会话壳子。

    点开就问、连点几次返回留下的空会话，会让审计页看起来像有人在乱用；
    演示前清一遍，列表里剩下的每一条都是真的问答记录。
    """
    n = (await db.execute(text(EMPTY_SQL))).scalar_one()
    print(f"空会话（无任何消息）：{n} 个")
    if apply and n:
        await db.execute(
            text(
                "DELETE FROM sessions WHERE NOT EXISTS "
                "(SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"
            )
        )
        await db.commit()
        print("✅ 已清理空会话")


async def run(apply: bool, keep: tuple[str, ...], empty_sessions: bool) -> int:
    async with SessionLocal() as db:
        if empty_sessions:
            await prune_empty_sessions(db, apply)

        users = (
            await db.execute(text("SELECT id, username, role, status FROM users ORDER BY id"))
        ).all()

        victims, survivors = [], []
        for uid, username, role, status in users:
            is_test = username.startswith(TEST_PREFIXES) and username not in keep
            (victims if is_test else survivors).append((uid, username, role, status))

        print(f"账号总数 {len(users)}｜待清理 {len(victims)}｜保留 {len(survivors)}")
        print("保留名单：" + ("、".join(u for _, u, _, _ in survivors) or "（无）"))

        if not victims:
            print("✅ 没有匹配到测试账号，无需清理")
            return 0
        ids = [v[0] for v in victims]
        qmarks = ",".join(f":id{i}" for i in range(len(ids)))
        params = {f"id{i}": v for i, v in enumerate(ids)}

        async def count(sql: str) -> int:
            return (await db.execute(text(sql.format(q=qmarks)), params)).scalar_one()

        counts = [
            ("sessions", f"SELECT COUNT(*) FROM sessions WHERE user_id IN ({qmarks})"),
            (
                "messages",
                "SELECT COUNT(*) FROM messages WHERE session_id IN "
                f"(SELECT id FROM sessions WHERE user_id IN ({qmarks}))",
            ),
            ("agent_runs", f"SELECT COUNT(*) FROM agent_runs WHERE user_id IN ({qmarks})"),
            (
                "agent_tool_calls",
                "SELECT COUNT(*) FROM agent_tool_calls WHERE run_id IN "
                f"(SELECT id FROM agent_runs WHERE user_id IN ({qmarks}))",
            ),
            (
                "agent_confirmations",
                "SELECT COUNT(*) FROM agent_confirmations WHERE run_id IN "
                f"(SELECT id FROM agent_runs WHERE user_id IN ({qmarks}))",
            ),
            (
                "chat_audit_marks",
                "SELECT COUNT(*) FROM chat_audit_marks WHERE marked_by IN "
                f"({qmarks}) OR revoked_by IN ({qmarks})",
            ),
            (
                "followup_task_drafts",
                f"SELECT COUNT(*) FROM followup_task_drafts WHERE created_by IN ({qmarks})",
            ),
        ]

        print("\n关联数据：")
        for label, sql_count in counts:
            print(f"  {label:<22}{await count(sql_count)}")

        if not apply:
            print("\n以上为预演。确认无误后加 --apply 执行。")
            return 0

        try:
            # 顺序不能改：消息依附会话、工具调用依附 run
            await db.execute(
                text(
                    f"DELETE FROM messages WHERE session_id IN "
                    f"(SELECT id FROM sessions WHERE user_id IN ({qmarks}))"
                ),
                params,
            )
            await db.execute(
                text(
                    f"DELETE FROM agent_tool_calls WHERE run_id IN "
                    f"(SELECT id FROM agent_runs WHERE user_id IN ({qmarks}))"
                ),
                params,
            )
            await db.execute(
                text(
                    f"DELETE FROM agent_confirmations WHERE run_id IN "
                    f"(SELECT id FROM agent_runs WHERE user_id IN ({qmarks}))"
                ),
                params,
            )
            await db.execute(
                text(f"DELETE FROM sessions WHERE user_id IN ({qmarks})"), params
            )
            await db.execute(
                text(f"DELETE FROM agent_runs WHERE user_id IN ({qmarks})"), params
            )
            await db.execute(
                text(
                    "DELETE FROM chat_audit_marks WHERE marked_by IN "
                    f"({qmarks}) OR revoked_by IN ({qmarks})"
                ),
                params,
            )
            await db.execute(
                text(f"DELETE FROM followup_task_drafts WHERE created_by IN ({qmarks})"),
                params,
            )
            await db.execute(text(f"DELETE FROM users WHERE id IN ({qmarks})"), params)

            # 护栏：删完必须还有可用管理员，否则回滚
            admins = (
                await db.execute(
                    text("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'")
                )
            ).scalar_one()
            if admins == 0:
                await db.rollback()
                print("❌ 清理后没有任何可用管理员，已回滚。请先补齐管理员再清理：")
                print("   python scripts/create_admin.py --username 你的名字 --password 你的密码")
                return 1

            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            print(f"❌ 清理失败，已回滚：{exc}")
            return 1

        left = (await db.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
        print(f"\n✅ 已删除 {len(victims)} 个测试账号，剩余账号 {left} 个（可用管理员 {admins} 个）")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="清理测试账号及其会话数据（幂等）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认只预演）")
    ap.add_argument("--keep", nargs="*", default=[], help="额外保留的登录名")
    ap.add_argument(
        "--empty-sessions", action="store_true", help="顺带清掉没有任何消息的空会话"
    )
    args = ap.parse_args()
    keep = tuple(KEEP_DEFAULT) + tuple(args.keep)
    return asyncio.run(run(args.apply, keep, args.empty_sessions))


if __name__ == "__main__":
    raise SystemExit(main())
