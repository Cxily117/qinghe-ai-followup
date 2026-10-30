"""全局配置：与阶段 1 原型的「环境变量配置」一致，升级为 pydantic-settings 版本。

阶段 2 起所有配置集中在 Settings，业务代码不直接读 os.environ。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "随访智能体系统"
    api_v1_prefix: str = "/api/v1"

    # 开发态默认 SQLite（零依赖起步）；生产切 PostgreSQL+pgvector
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # JWT
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720

    # ---- LLM 网关（D3–D4 起真正启用）----
    llm_provider: str = "mock"  # mock | openai（OpenAI 兼容：智谱/DeepSeek/通义/vLLM）
    llm_api_base: str = ""  # 如 https://open.bigmodel.cn/api/paas/v4
    llm_api_key: str = ""
    llm_chat_model: str = "glm-4-flash"
    llm_temperature: float = 0.2  # 制度问答要稳，温度调低
    llm_timeout: float = 120.0

    # ---- Embedding ----
    embed_provider: str = "mock"  # mock | openai
    embed_model: str = "embedding-3"
    embed_dim: int = 1024  # 必须与 pgvector 的 vector(N) 定义一致

    # ---- RAG ----
    chunk_size: int = 500  # 切片最大字符数（制度类中文文档 400~600 合适）
    chunk_overlap: int = 50  # 相邻切片重叠字符数，避免关键句被切断
    rag_top_k: int = 3  # 混合检索返回条数
    rag_alpha: float = 0.5  # 融合权重：向量分占比，BM25 占 1-alpha
    rag_max_candidates: int = 5000  # 单次检索扫描的切片上限（超过应改 pgvector ANN）

    # ---- 知识库存储 ----
    kb_storage_dir: str = "data/kb"  # 原始文档落盘目录（不入库，仅留档）

    @property
    def is_postgres(self) -> bool:
        """是否跑在 PostgreSQL 上——决定向量列用 pgvector 还是 JSON 降级。"""
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
