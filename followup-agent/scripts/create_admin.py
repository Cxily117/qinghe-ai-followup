# -*- coding: utf-8 -*-
"""管理员引导脚本：从服务器本地直接创建 / 补齐一个管理员账号。

什么时候用它：
  1. 首次部署 —— 想跳过「首个注册账号自动成为管理员」的引导，直接指定管理员；
  2. 忘记密码 / 唯一管理员被停用 —— 本地补救，不需要进后台；
  3. 冒烟测试 —— 注册接口只发医护后，测试专用管理员由这里落库（同机器直连库）。

用法（在项目根目录，需能读到 .env / DATABASE_URL）：
    python scripts/create_admin.py --username admin --password Passw0rd! --full-name 管理员
    python scripts/create_admin.py --username admin --password NewPass1!   # 已存在则重置密码并激活
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal  # noqa: E402
from app.services.user_service import ensure_admin  # noqa: E402


async def run(username: str, password: str, full_name: str) -> int:
    async with SessionLocal() as db:
        user = await ensure_admin(db, username=username, password=password, full_name=full_name)
        print(f"✅ 管理员就绪：id={user.id} username={user.username} role={user.role} status={user.status}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="创建或补齐一个管理员账号（幂等）")
    ap.add_argument("--username", required=True, help="登录名（≥3 字符）")
    ap.add_argument("--password", required=True, help="密码（≥6 字符）")
    ap.add_argument("--full-name", default="", help="姓名，可选")
    args = ap.parse_args()
    if len(args.username) < 3:
        print("❌ 用户名至少 3 个字符"); return 1
    if len(args.password) < 6:
        print("❌ 密码至少 6 个字符"); return 1
    return asyncio.run(run(args.username, args.password, args.full_name))


if __name__ == "__main__":
    raise SystemExit(main())
