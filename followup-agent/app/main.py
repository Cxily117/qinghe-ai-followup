"""应用入口：uvicorn app.main:app --reload

启动：
  cd followup-agent
  alembic upgrade head          # 建表（首次）
  uvicorn app.main:app --reload
页面：http://127.0.0.1:8000/        医护问答入口（单页，无构建）
文档：http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers

# web/ 与 app/ 同级：前端作为工程的一部分交付，不需要单独的构建链与 CORS
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动钩子：阶段 3 在此初始化 LLM 网关与向量索引缓存
    yield
    # 关闭钩子：阶段 3 在此释放连接


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version="0.6.0",
        description=(
            "医院随访智能体系统 · 阶段2（D1–D2 认证与数据模型；D3–D4 知识库 + 流式 RAG；"
            "D5 任务草稿；医护问答前端 + 统一错误码）"
        ),
        lifespan=lifespan,
    )

    # 统一错误 envelope：{code, message, request_id}（4xx/5xx 都一样）
    register_exception_handlers(app)
    app.include_router(api_router, prefix=s.api_v1_prefix)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            """医护问答入口。同域托管，前端直接用相对路径调 /api/v1/*。"""
            return FileResponse(WEB_DIR / "index.html")

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
