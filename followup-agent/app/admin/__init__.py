"""管理后台（服务端渲染）子包。

页面只用 Jinja2 + 一个 CSS 文件，不引入前端构建链：
后台是低频、长表单、重表格的场景，SSR 的收益（首屏无白屏、表单可靠、
不需要额外维护前端状态）远大于 SPA 的交互增益。
"""
from app.admin.routes import router

__all__ = ["router"]
