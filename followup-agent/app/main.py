"""应用入口：uvicorn app.main:app --reload

启动：
  cd followup-agent
  alembic upgrade head          # 建表（首次）
  uvicorn app.main:app --reload

页面：
  http://127.0.0.1:8000/            医护问答入口（单页，无构建）
  http://127.0.0.1:8000/admin       管理后台（服务端渲染，仅 admin）
  http://127.0.0.1:8000/docs        接口文档
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.admin import router as admin_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import register_exception_handlers
from app.services import prompt_service

logger = logging.getLogger(__name__)

# web/ 与 app/ 同级：医护端前端作为工程的一部分交付，不需要单独的构建链与 CORS
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ADMIN_STATIC = Path(__file__).resolve().parent / "admin" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时播种提示词：库里没有生效版本，问答就会拿空提示词去调模型
    try:
        async with SessionLocal() as db:
            await prompt_service.ensure_seed(db)
    except Exception:  # noqa: BLE001 —— 迁移没跑/库连不上时不该让服务起不来
        logger.warning("提示词种子初始化失败（可能尚未执行 alembic upgrade head）", exc_info=True)
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version="0.7.0",
        description=(
            "医院随访智能体系统 · 阶段3（管理后台：知识库管理 / 提示词在线编辑 / 对话审计；"
            "医护问答入口 + 统一错误码 + RBAC）"
        ),
        lifespan=lifespan,
    )

    # 统一错误 envelope：{code, message, request_id}（/admin 下的页面错误走 HTML 版）
    register_exception_handlers(app)
    app.include_router(api_router, prefix=s.api_v1_prefix)
    # 后台页面无 API 前缀：/admin、/admin/kb、/admin/prompts、/admin/audit
    app.include_router(admin_router)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            """医护问答入口。同域托管，前端直接用相对路径调 /api/v1/*。"""
            return FileResponse(WEB_DIR / "index.html")

    if ADMIN_STATIC.is_dir():
        app.mount("/admin/static", StaticFiles(directory=ADMIN_STATIC), name="admin-static")

    @app.get("/health", tags=["运维"], summary="健康检查")
    async def health():
        return {
            "status": "ok",
            "app": s.app_name,
            "db_dialect": s.database_url.split("://")[0],
            # 这两个字段是排障第一现场要看的东西：mock 状态下回答质量是无意义的
            "llm_provider": s.llm_provider,
            "embed_provider": s.embed_provider,
            "embed_dim": s.embed_dim,
        }

    return app


app = create_app()
