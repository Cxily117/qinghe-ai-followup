"""质控报表路由（管理员，D6）。

两个出口共用同一份 service 计算：
  - GET /summary      JSON（程序/看板用）
  - GET /summary.csv  CSV（质控台账导 Excel 用）
参数 days：统计窗口天数，钳制在 1-365，默认 30——
报表允许看任意窗口，但不允许一次拉全年以上把库拖死。
"""
from fastapi import APIRouter, Query, Response

from app.core.deps import AdminUser, DbDep
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["质控报表（管理员）"])


@router.get("/summary", summary="质控汇总（问答质量/草稿质量/智能体过程/审计标记）")
async def summary(admin: AdminUser, db: DbDep, days: int = Query(30, ge=1, le=365)):
    """staff 访问会在此被 require_admin 拦截返回 403——D1–D2 的 RBAC 验收点。"""
    data = await report_service.summary(db, days=days)
    data["operator"] = admin.username
    return data


@router.get("/summary.csv", summary="质控汇总导出 CSV（Excel 可直接打开）")
async def summary_csv(admin: AdminUser, db: DbDep, days: int = Query(30, ge=1, le=365)):
    data = await report_service.summary(db, days=days)
    csv_text = report_service.to_csv(data)
    # 加 BOM：Excel（尤其中文环境）没 BOM 会把 UTF-8 当 GBK 读出乱码
    return Response(
        content=b"\xef\xbb\xbf" + csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="qc-report-{days}d.csv"'
        },
    )
