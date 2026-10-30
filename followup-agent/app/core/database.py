"""数据库引擎与会话工厂（SQLAlchemy 2.0 异步）。"""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

# future=True 是 2.0 默认；echo 打开可看 SQL（调试用）
engine = create_async_engine(_settings.database_url, echo=False)

# expire_on_commit=False：提交后对象仍可直接读属性，避免接口序列化时再查库
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
