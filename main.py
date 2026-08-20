"""
FastAPI 入口
============
启动：cd backend && uvicorn main:app --reload --port 8000

lifespan 初始化（均为"尽力而为"，失败不阻塞启动）：
1. MySQL：建库 + 建表（失败 → 数据工具返回明确错误）
2. RAG：知识文档入库（失败 → 知识检索返回空，不注入模拟知识）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.chat import router as chat_router
from config.auth import ApiTokenMiddleware
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
        logger.warning("MySQL 初始化失败（%s）—— 数据工具将返回明确错误", exc)

    # 2) RAG 示例知识文档入库（失败 → 检索返回空，不降级模拟知识）
    try:
        n = ingest()
        logger.info("RAG 知识入库完成：%s chunks", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG 初始化失败（%s）—— 知识检索可能为空", exc)

    logger.info("服务就绪：llm=%s data_mode=real", settings.llm_provider)
    if not settings.api_token:
        logger.warning("BIZ_API_TOKEN 未配置——API 鉴权已禁用（仅限开发环境；生产/联调务必设置，见 config/auth.py）")
    yield


app = FastAPI(
    title="企业经营智能决策与自动化执行 Agent",
    description="数据获取 → 数据分析 → 问题诊断 → 策略生成 → 自动执行",
    version="0.1.0",
    lifespan=lifespan,
)

# API 鉴权中间件：配置 BIZ_API_TOKEN 后所有非公开接口需携带 Bearer Token（见 config/auth.py）
app.add_middleware(ApiTokenMiddleware, token=settings.api_token)

app.include_router(chat_router)

# 可视化前端（静态页面）
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/data", include_in_schema=False)
def data_workspace() -> FileResponse:
    """数据采集工作台。"""
    return FileResponse(BASE_DIR / "static" / "data.html")


@app.get("/knowledge", include_in_schema=False)
def knowledge_workspace() -> FileResponse:
    """RAG 知识库管理与上传页。"""
    return FileResponse(BASE_DIR / "static" / "knowledge.html")


@app.get("/sessions", include_in_schema=False)
def sessions_workspace() -> FileResponse:
    """独立历史会话页。"""
    return FileResponse(BASE_DIR / "static" / "sessions.html")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "business-agent",
        "llm_provider": settings.llm_provider,
        "database": check_connection(),
    }


# 直接运行入口：python main.py（或 VSCode 右键 Run Python File / F5）
# 注意：走 codebuddy 通道时需先启动 workbuddy2api（8788/8787），见 start-all.sh
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
