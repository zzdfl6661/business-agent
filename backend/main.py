"""
FastAPI 入口
============
启动：cd backend && uvicorn main:app --reload --port 8000

lifespan 初始化（均为"尽力而为"，失败不阻塞启动）：
1. MySQL：建库 + 建表（失败 → 数据工具自动降级 Mock）
2. RAG：示例知识文档入库（Mock 模式跳过；失败 → 检索降级内置示例）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.chat import router as chat_router
from config.logging_setup import setup_logging
from config.settings import settings
from database.mysql import check_connection, init_db
from rag.loader import ingest

# 日志系统：控制台 + logs/ 按天滚动文件（首次 import 即配置）
setup_logging()
logger = logging.getLogger("main")

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 1) MySQL 初始化（尽力而为）
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning("MySQL 初始化失败（%s）—— 数据工具将降级为 Mock 数据", exc)

    # 2) RAG 示例文档入库（Mock 模式跳过，检索走内置示例知识）
    try:
        if settings.mock_mode:
            logger.info("Mock 模式：跳过 RAG 入库（检索使用内置示例知识）")
        else:
            n = ingest()
            logger.info("RAG 示例知识入库完成：%s chunks", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG 初始化失败（%s）—— 检索将降级为内置示例知识", exc)

    logger.info("服务就绪：llm=%s mock=%s data_mode=%s", settings.llm_provider, settings.mock_mode, settings.data_mode)
    yield


app = FastAPI(
    title="企业经营智能决策与自动化执行 Agent",
    description="数据获取 → 数据分析 → 问题诊断 → 策略生成 → 自动执行",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(chat_router)

# 可视化前端（静态页面）
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "business-agent",
        "llm_provider": settings.llm_provider,
        "mock_mode": settings.mock_mode,
        "data_mode": settings.data_mode,
        "database": check_connection(),
    }
