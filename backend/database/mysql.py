"""
MySQL 连接层
============
- create_db_if_not_exists() : 数据库不存在时自动创建
- init_db()                 : 建库 + 建表（幂等，create_all 仅建缺失表）
- get_session()             : FastAPI 依赖注入用的 session 生成器
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from database.models import Base

logger = logging.getLogger(__name__)

_engine = None
_session_factory: sessionmaker | None = None


def _server_dsn() -> str:
    """不带库名的连接串（用于 CREATE DATABASE）。"""
    return (
        f"mysql+pymysql://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/?charset=utf8mb4"
    )


def create_db_if_not_exists() -> None:
    import pymysql  # 轻量导入，避免链式依赖

    conn = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.db_name}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.db_url,
            pool_size=5,
            max_overflow=10,
            pool_recycle=3600,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def init_db() -> None:
    """建库 + 建表。数据库不可用时抛出异常，由调用方决定降级策略。"""
    create_db_if_not_exists()
    get_engine().connect()  # 提前暴露连接问题
    Base.metadata.create_all(get_engine())
    logger.info("数据库初始化完成: %s@%s:%s/%s", settings.db_user, settings.db_host, settings.db_port, settings.db_name)


def get_session():
    """FastAPI 依赖注入：提供 Session 作用域。"""
    with get_session_factory()() as session:
        yield session


def check_connection() -> bool:
    """连通性探测（/health 使用）。"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
