"""认证路由：注册 / 登录 / 当前用户 / 退出。"""
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import TOKEN_COOKIE, CurrentUser, DbDep
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import (
    PasswordChange,
    TokenOut,
    UserLogin,
    UserOut,
    UserRegister,
)

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             summary="注册用户（医护）")
async def register(payload: UserRegister, db: DbDep):
    exists = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    # 冷启动引导（bootstrap）：系统一个管理员都没有时，首个注册账号自动成为管理员。
    # 只在零管理员时开放一次——之后管理员只能由后台「用户与授权」页授予，
    # 或服务器本地跑 scripts/create_admin.py。否则会回到「人人可自注册 admin」的老问题。
    has_admin = (
        await db.execute(select(User.id).where(User.role == "admin").limit(1))
    ).scalar_one_or_none() is not None
    role = "staff" if has_admin else "admin"

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=role,
        department=payload.department,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut, summary="登录，换取 JWT")
async def login(payload: UserLogin, request: Request, response: Response, db: DbDep):
    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    # 统一提示「用户名或密码错误」，不泄露用户是否存在
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用")

    s = get_settings()
    token = create_access_token(user.id, user.role)
    # 同时下发 HttpOnly Cookie：浏览器后续请求自动携带，不依赖前端存 localStorage，
    # 也不怕反向代理改写 Authorization 头。httponly=True 让 JS 读不到，XSS 偷不走。
    forwarded = request.headers.get("x-forwarded-proto", "")
    is_https = request.url.scheme == "https" or forwarded == "https"
    response.set_cookie(
        key=TOKEN_COOKIE,
        value=token,
        max_age=s.access_token_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=is_https,  # 本地走 http，加了 Secure 浏览器会直接丢弃该 Cookie
        path="/",
    )
    return TokenOut(access_token=token)


@router.post("/logout", summary="退出登录（清 Cookie）")
async def logout(response: Response):
    """前端退出时调用：清掉 HttpOnly Cookie，服务端无需维护黑名单。"""
    response.delete_cookie(TOKEN_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut, summary="当前登录用户信息")
async def me(user: CurrentUser):
    return user


@router.post("/password", summary="修改自己的密码")
async def change_password(payload: PasswordChange, user: CurrentUser, db: DbDep):
    """自助改密码：验证旧密码 → 校验新密码 → 落库。

    为什么必须验旧密码：浏览器里的登录态（HttpOnly Cookie）可能被他人借用，
    只凭「已登录」就允许改密码，等于坐在你电脑前的人能直接接管账号。

    已知取舍：JWT 是无状态的，改密码不会让已签发的 token 立即失效
    （有效期 12 小时，到期自然失效）。要做到「改密即踢下线」需要给用户表
    加 token 版本号并在鉴权时比对，属于后续加固项，此处不为改密码引入新表结构。
    """
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确"
        )
    if payload.old_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同"
        )
    user.password_hash = hash_password(payload.new_password)
    await db.commit()
    return {"ok": True, "message": "密码已修改，下次登录请使用新密码"}
