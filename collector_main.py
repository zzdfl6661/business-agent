"""Windows 宿主机数据采集器：唯一持有 Edge、CDP 登录态和 Playwright。"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from config.auth import ApiTokenMiddleware
from config.logging_setup import audit, setup_logging
from config.settings import settings
from tools.data_ingest_tool import refresh_market_data

setup_logging()
logger = logging.getLogger("collector")
_refresh_lock = asyncio.Lock()
_dws_lock = asyncio.Lock()

app = FastAPI(
    title="Business Agent Windows Collector",
    description="仅在 Windows 宿主机运行，负责 Edge 登录态的数据采集。",
    version="0.1.0",
)
app.add_middleware(ApiTokenMiddleware, token=settings.api_token)


class CollectorRefreshRequest(BaseModel):
    datasets: str = Field("campaign", max_length=80)
    port: int = Field(9222, ge=1, le=65535)


class DingTalkSendRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1, max_length=1200)
    idempotency_key: str = Field(..., min_length=8, max_length=128)


class DingTalkUserSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=80)


def _run_dws(args: list[str]) -> dict:
    executable = settings.dingtalk_dws_command
    resolved_executable = shutil.which(executable)
    if not resolved_executable:
        raise RuntimeError(f"找不到 DWS 命令：{executable}；请先在 Windows 安装并登录 dingtalk-workspace-cli")
    if Path(resolved_executable).suffix.lower() in {".cmd", ".bat"}:
        raise RuntimeError(
            "请将 BIZ_DINGTALK_DWS_COMMAND 配置为 dingtalk-workspace-cli/vendor/dws.exe；"
            "通过 dws.cmd 转发多行正文会被 Windows 截断"
        )
    completed = subprocess.run(
        [executable, *args, "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip().replace("\n", " ")
        raise RuntimeError(f"DWS 调用失败：{detail[:220]}")
    raw = (completed.stdout or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("DWS 未返回可解析的 JSON 结果") from exc


def _dws_send_text_args(open_dingtalk_id: str, text: str, idempotency_key: str) -> list[str]:
    """Build arguments for the current DWS personal-chat command.

    The native DWS executable accepts ``--content`` for the message body and
    ``--idempotency-key`` for 24-hour deduplication.  It must be invoked
    directly rather than through ``dws.cmd`` so Windows preserves newlines.
    """
    return [
        "chat", "message", "send", "--open-dingtalk-id", open_dingtalk_id,
        "--content", text, "--idempotency-key", idempotency_key,
    ]


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "business-agent-windows-collector",
        "dws_available": bool(shutil.which(settings.dingtalk_dws_command)),
    }


@app.post("/api/collector/refresh")
async def refresh(req: CollectorRefreshRequest) -> dict:
    if _refresh_lock.locked():
        return {"success": False, "data": {}, "error": "采集器已有刷新任务进行中"}
    async with _refresh_lock:
        try:
            result = await refresh_market_data(req.datasets, req.port)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Collector 刷新失败")
            result = {"success": False, "data": {}, "error": f"采集器内部异常：{str(exc)[:150]}"}
    audit("collector_refresh", None, datasets=req.datasets, success=result.get("success"))
    return result


@app.post("/api/collector/dingtalk/search-user")
async def search_dingtalk_user(req: DingTalkUserSearchRequest) -> dict:
    """仅供首次确认朱兴福 userId；不发送消息。"""
    async with _dws_lock:
        try:
            result = await asyncio.to_thread(
                _run_dws, ["contact", "user", "search", "--query", req.query]
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)[:240]}
    return {"success": True, "result": result}


@app.post("/api/collector/dingtalk/send-text")
async def send_dingtalk_text(req: DingTalkSendRequest) -> dict:
    """DWS 个人账号单聊发送器：只允许配置中锁定的朱兴福 userId。"""
    if not settings.dingtalk_enabled or settings.dingtalk_mode != "live":
        return {"success": False, "error": "钉钉真实发送未启用"}
    if not settings.dingtalk_live_recipient_user_id:
        return {"success": False, "error": "未配置朱兴福的钉钉 userId"}
    if not settings.dingtalk_live_recipient_open_id:
        return {"success": False, "error": "未配置朱兴福的钉钉 openDingTalkId"}
    if req.user_id != settings.dingtalk_live_recipient_user_id:
        return {"success": False, "error": "收件人不匹配，拒绝发送"}
    async with _dws_lock:
        try:
            result = await asyncio.to_thread(
                _run_dws,
                _dws_send_text_args(
                    settings.dingtalk_live_recipient_open_id,
                    req.text,
                    req.idempotency_key,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("DWS 私聊发送失败")
            return {"success": False, "error": str(exc)[:240]}
    audit("dws_personal_message_sent", None, recipient="朱兴福", idempotency_key=req.idempotency_key)
    return {"success": True, "result": result}
