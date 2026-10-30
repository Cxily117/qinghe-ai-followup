"""ORM 基类与「向量列类型」的双方言选择策略。

关键取舍：PostgreSQL 用 pgvector 的 Vector(1024) 做 ANN 检索；
开发态 SQLite 无此类型，降级为 JSON（只存不检，够 D1–D2 建表与联调）。
"""
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有模型的基类（SQLAlchemy 2.0 风格）。"""


def embedding_column_type():
    """按数据库方言返回向量列类型。调用时机：模型 import 时（配置已就绪）。"""
    from app.core.config import get_settings

    s = get_settings()
    if s.is_postgres:
        from pgvector.sqlalchemy import Vector

        return Vector(s.embed_dim)
    return sa.JSON()  # SQLite 开发态降级：仅存储，不做向量检索
