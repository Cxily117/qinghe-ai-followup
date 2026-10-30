"""FastAPI 依赖注入：数据库会话、当前用户、管理员校验。

RBAC 设计（对应需求清单「三角色、多科室」的最小实现）：
  - staff：医护，可问答/发起任务草稿/录记录
  - admin：管理员，额外可维护知识库、看质控报表
  科室级数据隔离在 D6 引入（当前先落 role 字段与 department 字段）。
"""
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.security import decode_token
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


async def get_db():
    """每个请求一个独立会话，请求结束自动关闭。"""
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析 Bearer Token → 校验 → 查库确认用户有效。

    注意：角色以数据库为准，不信任 token 里的 role（防止改角色后旧 token 越权）。
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或登录已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError:
        raise unauthorized

    user = await db.get(User, int(payload["sub"]))
    if user is None or user.status != "active":
        raise unauthorized
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """管理员专用依赖：非 admin 一律 403。"""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


# 类型别名，路由签名更简洁
DbDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
