"""
日志与审计系统
==============
1. 文件日志：logs/ 目录按天滚动（TimedRotatingFileHandler），保留控制台输出
2. 审计日志：audit_logs 表——记录关键操作（对话/工具调用/自动执行），供追溯

用法：
    from config.logging_setup import setup_logging, audit
    setup_logging()          # 应用启动时调用一次
    audit("chat", session_id, {"question": ...})   # 记录业务事件
"""
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BACKEND_DIR / "logs"

_AUDIT_LOGGER = logging.getLogger("audit")


def setup_logging(level: int = logging.INFO) -> None:
    """配置根日志：控制台 + 按天滚动文件（logs/app-YYYYMMDD.log）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 避免重复添加 handler（热重载场景）
    if any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        return

    file_handler = TimedRotatingFileHandler(
        LOG_DIR / "app.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 审计日志单独文件（audit.log），便于独立检索
    audit_handler = TimedRotatingFileHandler(
        LOG_DIR / "audit.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    audit_handler.setFormatter(fmt)
    _AUDIT_LOGGER.addHandler(audit_handler)
    _AUDIT_LOGGER.setLevel(logging.INFO)
    _AUDIT_LOGGER.propagate = False


def audit(event_type: str, session_id: str | None = None, **detail) -> None:
    """记录审计事件：写 audit.log + MySQL audit_logs 表（表不可用时降级文件日志）。"""
    payload = {"event": event_type, "session_id": session_id, **detail}
    _AUDIT_LOGGER.info(json.dumps(payload, ensure_ascii=False, default=str))

    try:
        from database.mysql import get_session_factory
        from database.models import AuditLog

        with get_session_factory()() as session:
            session.add(AuditLog(
                event_type=event_type[:32],
                session_id=session_id[:64] if session_id else None,
                detail=json.dumps(payload, ensure_ascii=False, default=str)[:4000],
            ))
            session.commit()
    except Exception as exc:  # noqa: BLE001 审计失败不影响主流程
        logging.getLogger("audit").warning("审计入库失败（仅保留文件日志）：%s", exc)
