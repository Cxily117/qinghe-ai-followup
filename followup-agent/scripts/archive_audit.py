"""审计日志归档：把超过在线保留期的会话、消息、运行记录导出后删除。

为什么必须有（对应上线清单 S3）：
  两头都不合规——不清理，库会无限膨胀（每次问答都留会话 + 运行 + 工具调用）；
  直接删，则审计链断裂，出问题查不回去。正确做法是「冷归档 + 在线只留窗口期」：
  在线保留 ≥6 个月（按院内策略可调），归档件保留 ≥3 年。

用法：
  python scripts/archive_audit.py                     # 预演，只统计不动作
  python scripts/archive_audit.py --days 180 --apply  # 真正导出并删除
  python scripts/archive_audit.py --days 180 --apply --keep-marks
                                                      # 保留人工标记（默认就保留）

产出：
  data/archive/audit-YYYYMMDD-HHMM.jsonl.gz   归档内容（每行一条记录，带表名）
  data/archive/manifest.jsonl                 每次操作追加一行：时间/范围/条数/文件 sha256

不归档什么：`chat_audit_marks`（人工质控标记）与 `prompt_templates`（提示词版本）
是合规证据，只统计不删除；要删必须人工决定并留书面记录。
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "archive"

# 顺序即删除顺序：先子表后主表，避免外键报错
TABLES = [
    (
        "agent_tool_calls",
        "SELECT * FROM agent_tool_calls WHERE run_id IN "
        "(SELECT id FROM agent_runs WHERE created_at < :cutoff) ORDER BY id",
    ),
    ("agent_runs", "SELECT * FROM agent_runs WHERE created_at < :cutoff ORDER BY id"),
    (
        "messages",
        "SELECT * FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE created_at < :cutoff) ORDER BY id",
    ),
    ("sessions", "SELECT * FROM sessions WHERE created_at < :cutoff ORDER BY id"),
]


def _dump(rows) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r._mapping)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat(sep=" ", timespec="seconds")
        out.append(d)
    return out


async def run(days: int, apply: bool) -> int:
    cutoff = datetime.now() - timedelta(days=days)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    async with SessionLocal() as db:
        print(f"在线保留期：{days} 天　截止时间：{cutoff:%Y-%m-%d %H:%M}")
        payload: list[str] = []
        counts: dict[str, int] = {}

        for table, sql in TABLES:
            rows = (await db.execute(text(sql), {"cutoff": cutoff})).all()
            counts[table] = len(rows)
            for item in _dump(rows):
                payload.append(json.dumps({"table": table, "row": item}, ensure_ascii=False))

        marks = (await db.execute(text("SELECT COUNT(*) FROM chat_audit_marks"))).scalar() or 0
        print("\n待归档：")
        for table, n in counts.items():
            print(f"  {table:<20}{n}")
        print(f"  chat_audit_marks{'':<4}{marks}（合规证据，只统计不删除）")

        if not any(counts.values()):
            print("\n✅ 没有超过保留期的数据，无需归档")
            return 0

        if not apply:
            print("\n以上为预演。确认后加 --apply 真正执行。")
            return 0

        path = ARCHIVE_DIR / f"audit-{stamp}.jsonl.gz"
        # 先落盘、再校验、最后才删：顺序反了，中途失败就是数据丢失
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write("\n".join(payload) + "\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"\n已导出：{path.name}（{len(payload)} 条，sha256={digest[:16]}…）")

        await db.execute(
            text(
                "DELETE FROM agent_tool_calls WHERE run_id IN "
                "(SELECT id FROM agent_runs WHERE created_at < :cutoff)"
            ),
            {"cutoff": cutoff},
        )
        await db.execute(text("DELETE FROM agent_runs WHERE created_at < :cutoff"), {"cutoff": cutoff})
        await db.execute(
            text(
                "DELETE FROM messages WHERE session_id IN "
                "(SELECT id FROM sessions WHERE created_at < :cutoff)"
            ),
            {"cutoff": cutoff},
        )
        await db.execute(text("DELETE FROM sessions WHERE created_at < :cutoff"), {"cutoff": cutoff})
        await db.commit()

        manifest = ARCHIVE_DIR / "manifest.jsonl"
        with manifest.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                        "retention_days": days,
                        "cutoff": cutoff.isoformat(sep=" ", timespec="seconds"),
                        "file": path.name,
                        "sha256": digest,
                        "rows": counts,
                        "operator": "scripts/archive_audit.py",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        print(f"已删除在线数据，操作记录见 {manifest.name}")
        print("⚠️ 归档件请转存到院内归档存储（本目录不参与数据库备份）")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="审计数据冷归档")
    ap.add_argument("--days", type=int, default=180, help="在线保留天数（默认 180）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预演）")
    args = ap.parse_args()
    return asyncio.run(run(args.days, args.apply))


if __name__ == "__main__":
    sys.exit(main())
