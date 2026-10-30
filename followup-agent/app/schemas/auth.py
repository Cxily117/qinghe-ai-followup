"""认证相关 DTO。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UserRegister(BaseModel):
    username: str = Field(min_length=3, max_length=64, description="登录名")
    password: str = Field(min_length=6, max_length=72, description="密码，至少 6 位")
    full_name: str = Field(default="", max_length=64)
    # 注册只产生医护账号：管理员只能由已有管理员在后台「用户与授权」页授予，
    # 或冷启动时由系统引导（见 auth.register 的 bootstrap 与 user_service.ensure_admin）。
    # 之前 Literal["staff","admin"] 的口子已收掉——任何人都能自注册管理员 = 权限模型失效。
    role: Literal["staff"] = "staff"
    department: str | None = Field(default=None, max_length=64)


class UserLogin(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str
    role: str
    department: str | None
