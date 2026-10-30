"""认证相关 DTO。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UserRegister(BaseModel):
    username: str = Field(min_length=3, max_length=64, description="登录名")
    password: str = Field(min_length=6, max_length=72, description="密码，至少 6 位")
    full_name: str = Field(default="", max_length=64)
    # 演示阶段允许注册 admin；生产环境应改为仅后台开通
    role: Literal["staff", "admin"] = "staff"
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
