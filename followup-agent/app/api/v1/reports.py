"""质控报表路由（管理员）：用于验证 RBAC 的 403 行为。"""
from fastapi import APIRouter

from app.core.deps import AdminUser

router = APIRouter(prefix="/reports", tags=["质控报表（管理员）"])


@router.get("/summary", summary="质控汇总（占位）")
async def summary(admin: AdminUser):
    """staff 访问会在此被 require_admin 拦截返回 403——D1–D2 的 RBAC 验收点。"""
    return {
        "message": "质控报表将在 D6 实现",
        "operator": admin.username,
        "metrics": {"完成率": None, "红旗升级数": None},
    }
