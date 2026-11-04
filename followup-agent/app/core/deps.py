"""FastAPI 依赖注入：数据库会话、当前用户、管理员校验。

RBAC 设计（对应需求清单「三角色、多科室」的最小实现）：
  - staff：医护，可问答/发起任务草稿/录记录
  - admin：管理员，额外可维护知识库、看质控报表
  科室级数据隔离在 D6 引入（当前先落 role 字段与 department 字段）。
"""
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.core.security import decode_token
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)

# 令牌的 Cookie 名。与后台的 qh_admin 分开：问答端与管理端各自独立登录态。
# 为什么要有 Cookie 这条路：部分托管/网关会占用或改写 Authorization 头
# （实测某平台把客户端发来的 47 字符头换成它自己的 386 字符令牌），
# 那时 Bearer 永远到不了应用，所有带鉴权的接口都 401。Cookie 不会被改写。
TOKEN_COOKIE = "qh_token"


async def get_db():
    """每个请求一个独立会话，请求结束自动关闭。"""
    async with SessionLocal() as session:
        yield session


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析令牌 → 校验 → 查库确认用户有效。

    令牌来源按顺序尝试，任一通过即放行：
      1. `Authorization: Bearer <jwt>`（标准做法，本地与脚本调用都走这条）
      2. Cookie `qh_token`（浏览器；网关会改写 Authorization 时靠这条兜底）
      3. 自定义头 `X-Auth-Token`（部分代理会保留自定义头）

    注意：角色以数据库为准，不信任 token 里的 role（防止改角色后旧 token 越权）。
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录或登录已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )

    candidates: list[str] = []
    if credentials is not None and credentials.credentials:
        candidates.append(credentials.credentials)
    cookie_token = request.cookies.get(TOKEN_COOKIE)
    if cookie_token:
        candidates.append(cookie_token)
    header_token = request.headers.get("x-auth-token")
    if header_token:
        candidates.append(header_token)

    for token in candidates:
        try:
            payload = decode_token(token)
            user_id = int(payload["sub"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            # 网关塞进来的令牌不是我们签的，或格式不符：换下一个来源继续试
            continue
        user = await db.get(User, user_id)
        if user is not None and user.status == "active":
            return user
    raise unauthorized


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """管理员专用依赖：非 admin 一律 403。"""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


# 类型别名，路由签名更简洁
DbDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
