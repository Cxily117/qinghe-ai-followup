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
    # 启动自检：把真正生效的模型配置打出来。
    # "大模型连接失败" 这类报障，第一件事就是确认供应商/模型/base/key 到没到位——
    # 有这行日志，不用翻 .env 也不用猜。
    s = get_settings()
    key_state = f"已配置（{s.llm_api_key[:6]}…，{len(s.llm_api_key)} 位）" if s.llm_api_key else "❌ 未配置"
    logger.warning(
        "LLM 配置：provider=%s model=%s base=%s key=%s | embedding=%s/%s(%d 维)",
        s.llm_provider,
        s.llm_chat_model,
        s.llm_api_base or "(默认)",
        key_state,
        s.embed_provider,
        s.embed_model,
        s.embed_dim,
    )
    if s.llm_provider == "openai" and not s.llm_api_key:
        logger.warning("LLM_PROVIDER=openai 但 LLM_API_KEY 为空，问答会直接失败")

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
            """医护问答入口。同域托管，前端直接用相对路径调 /api/v1/*。

            HTML 必须禁缓存：前端发版只升静态资源版本号（?v=px），
            若 HTML 本身被浏览器缓存，就会引用旧版本号 → 用户永远拿到旧样式。
            """
            return FileResponse(
                WEB_DIR / "index.html",
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

    if ADMIN_STATIC.is_dir():
        app.mount("/admin/static", StaticFiles(directory=ADMIN_STATIC), name="admin-static")

    @app.middleware("http")
    async def _no_cache_admin_html(request, call_next):
        """后台 HTML 禁缓存：后台发版加了导航项/新页面，若 base.html 被浏览器缓存，
        用户会一直看到旧侧边栏（"新功能在哪"的又一来源）。静态资源带版本号不受影响。"""
        response = await call_next(request)
        if request.url.path.startswith("/admin") and "text/html" in response.headers.get(
            "content-type", ""
        ):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

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
