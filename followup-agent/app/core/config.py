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

    # LLM（阶段 3 使用，D1–D2 仅占位）
    llm_provider: str = "mock"
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_chat_model: str = "glm-4-flash"
    embed_model: str = "embedding-3"
    embed_dim: int = 1024

    @property
    def is_postgres(self) -> bool:
        """是否跑在 PostgreSQL 上——决定向量列用 pgvector 还是 JSON 降级。"""
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
