"""用户账号与授权服务（管理员系统）。

角色模型（RBAC 最小实现）：
  staff —— 医护，注册即得，问答 / 任务草稿 / 记录；
  admin —— 管理员，额外可维护知识库、提示词、审计、报表，以及本模块的「用户与授权」。

为什么注册接口不再允许自选 admin（本模块存在的理由）：
  任何能看到注册页的人都能给自己发管理员 = 权限模型形同虚设。所以改为：
  注册只能产生医护；管理员身份只能由「已有管理员在后台授权」或
  「冷启动引导（首个注册账号 / 运维脚本）」产生，且全程落审计可查。

防呆三原则（授权动作的前置校验，set_role / set_status 共用）：
  1. 不能操作自己 —— 管理员把自己降级/停用 = 当场把自己锁在门外；
  2. 系统必须始终保留至少一名可用（active）管理员 —— 最后一个 admin 不能降级或停用；
  3. 目标必须存在且参数合法 —— 非法值在入口就拒绝，不落库。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.models.user import User

ROLES = ("staff", "admin")
STATUSES = ("active", "disabled")


async def count_active_admins(db: AsyncSession) -> int:
    """当前可用的管理员数量——「最后一个 admin 不可降级/停用」的判断依据。"""
    result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.status == "active")
    )
    return int(result.scalar_one())


async def get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise AppError("NOT_FOUND")
    return user


async def set_role(db: AsyncSession, target: User, role: str, operator: User) -> User:
    """授权：改角色。含防呆校验，全部通过才落库。"""
    if role not in ROLES:
        raise AppError("VALIDATION_ERROR", message="角色只能是 staff（医护）或 admin（管理员）")
    if target.id == operator.id:
        raise AppError("FORBIDDEN", message="不能修改自己的角色")
    if target.role == "admin" and role != "admin":
        if target.status == "active" and await count_active_admins(db) <= 1:
            raise AppError(
                "CONFLICT",
                message="系统至少要保留一名可用管理员，请先把其他账号授权为管理员",
            )
    target.role = role
    await db.commit()
    return target


async def set_status(db: AsyncSession, target: User, status: str, operator: User) -> User:
    """启停账号：停用后该账号立即无法登录（登录与页面鉴权都以库中 status 为准）。"""
    if status not in STATUSES:
        raise AppError("VALIDATION_ERROR", message="状态只能是 active（启用）或 disabled（停用）")
    if target.id == operator.id:
        raise AppError("FORBIDDEN", message="不能停用自己的账号")
    if target.role == "admin" and status == "disabled":
        if await count_active_admins(db) <= 1:
            raise AppError(
                "CONFLICT",
                message="系统至少要保留一名可用管理员，请先把其他账号授权为管理员",
            )
    target.status = status
    await db.commit()
    return target


async def reset_password(
    db: AsyncSession, target: User, new_password: str, operator: User
) -> User:
    """管理员重置他人密码（忘记密码 / 交接账号时用）。

    权限边界：
      - 调用点已在后台页面用 _guard 校验 role=admin（页面 + 表单两道闸门），
        这里再做一次身份校验，避免有人绕开页面直接打接口；
      - 管理员不能重置自己的密码 —— 自己改密码走问答页「设置」（要验旧密码），
        否则「已登录即可改密」等于绕过了旧密码校验。
    """
    if operator.role != "admin" or operator.status != "active":
        raise AppError("FORBIDDEN", message="仅管理员可重置他人密码")
    if target.id == operator.id:
        raise AppError("FORBIDDEN", message="修改自己的密码请用问答页右上角「设置」")
    if not (6 <= len(new_password) <= 72):
        raise AppError("VALIDATION_ERROR", message="密码长度需在 6-72 位之间")
    target.password_hash = hash_password(new_password)
    await db.commit()
    return target


async def ensure_admin(
    db: AsyncSession, *, username: str, password: str, full_name: str = ""
) -> User:
    """冷启动引导：创建或「补齐」一个管理员。

    幂等，两种用途：
      1. scripts/create_admin.py —— 首次部署 / 忘记密码时，从服务器本地直接建号；
      2. 冒烟测试 —— 新约束下注册接口不再发 admin，测试账号由这里直接落库。
    已存在则重置密码、提升为 admin 并激活（不重复插入）。
    """
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name or username,
            role="admin",
            status="active",
        )
        db.add(user)
    else:
        user.password_hash = hash_password(password)
        user.role = "admin"
        user.status = "active"
        if full_name:
            user.full_name = full_name
    await db.commit()
    await db.refresh(user)
    return user
