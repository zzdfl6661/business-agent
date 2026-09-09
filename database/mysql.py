"""
MySQL 连接层
============
- create_db_if_not_exists() : 数据库不存在时自动创建
- init_db()                 : 建库 + 建表（幂等，create_all 仅建缺失表）
- get_session()             : FastAPI 依赖注入用的 session 生成器
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import create_engine, inspect, text
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
    _apply_lightweight_migrations()
    logger.info("数据库初始化完成: %s@%s:%s/%s", settings.db_user, settings.db_host, settings.db_port, settings.db_name)


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"非法 SQL 标识符: {value}")
    return value


def _apply_lightweight_migrations() -> None:
    """为已有本地 MySQL 数据库补充小型兼容迁移。

    项目没有引入 Alembic；这里只处理本次新增列和历史快照唯一索引，且全部先
    通过 inspector 校验现状，重复启动不会重复执行。
    """
    engine = get_engine()
    if engine.dialect.name != "mysql":
        return
    inspector = inspect(engine)
    with engine.begin() as conn:
        store_cols = {c["name"] for c in inspector.get_columns("stores")}
        if "platform_store_id" not in store_cols:
            conn.execute(text("ALTER TABLE stores ADD COLUMN platform_store_id BIGINT NULL"))
            conn.execute(text("CREATE INDEX ix_stores_platform_store_id ON stores (platform_store_id)"))

        promo_cols = {c["name"] for c in inspector.get_columns("promotion_reports")}
        if "store_id" not in promo_cols:
            conn.execute(text("ALTER TABLE promotion_reports ADD COLUMN store_id BIGINT NULL"))
            conn.execute(text("CREATE INDEX ix_promotion_reports_store_id ON promotion_reports (store_id)"))
        if "store_name" not in promo_cols:
            conn.execute(text("ALTER TABLE promotion_reports ADD COLUMN store_name VARCHAR(128) NULL"))

        for idx in inspector.get_indexes("data_snapshots"):
            cols = idx.get("column_names") or []
            if idx.get("unique") and cols == ["dataset"]:
                name = _safe_identifier(idx["name"])
                conn.execute(text(f"ALTER TABLE data_snapshots DROP INDEX `{name}`"))
                break

        notification_cols = {c["name"] for c in inspector.get_columns("notification_plans")}
        notification_additions = {
            "version": "BIGINT NOT NULL DEFAULT 1",
            "dispatch_idempotency_key": "VARCHAR(96) NULL",
            "approved_by": "VARCHAR(128) NULL",
            "cancelled_by": "VARCHAR(128) NULL",
            "last_request_id": "VARCHAR(64) NULL",
        }
        for column, ddl in notification_additions.items():
            if column not in notification_cols:
                conn.execute(text(f"ALTER TABLE notification_plans ADD COLUMN {column} {ddl}"))

        notification_inspector = inspect(engine)
        notification_indexes = notification_inspector.get_indexes("notification_plans")
        notification_uniques = notification_inspector.get_unique_constraints("notification_plans")
        has_dispatch_unique = any(
            idx.get("unique") and idx.get("column_names") == ["dispatch_idempotency_key"]
            for idx in notification_indexes
        ) or any(
            item.get("column_names") == ["dispatch_idempotency_key"]
            for item in notification_uniques
        )
        if not has_dispatch_unique:
            conn.execute(text(
                "CREATE UNIQUE INDEX ux_notification_dispatch_idempotency "
                "ON notification_plans (dispatch_idempotency_key)"
            ))
        has_request_index = any(
            idx.get("column_names") == ["last_request_id"] for idx in notification_indexes
        )
        if not has_request_index:
            conn.execute(text(
                "CREATE INDEX ix_notification_last_request_id ON notification_plans (last_request_id)"
            ))


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
