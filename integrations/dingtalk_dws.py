"""通过 Windows 宿主机 DWS CLI 发送个人钉钉单聊消息。"""
from __future__ import annotations

from typing import Any

import httpx

from config.settings import settings


class DingTalkDWSError(RuntimeError):
    pass


class DingTalkDWSHostClient:
    """Docker 内主服务到 Windows 宿主机 DWS 执行器的受限客户端。"""

    def _endpoint(self) -> str:
        if not settings.collector_url:
            raise DingTalkDWSError("未配置 Windows 宿主机 Collector，无法调用个人钉钉 DWS")
        return f"{settings.collector_url.rstrip('/')}/api/collector/dingtalk/send-text"

    def send_text(self, user_id: str, text: str, idempotency_key: str) -> dict[str, Any]:
        if not settings.dingtalk_enabled or settings.dingtalk_mode != "live":
            raise DingTalkDWSError("钉钉真实发送未启用（需 BIZ_DINGTALK_ENABLED=true 且 mode=live）")
        if not settings.dingtalk_live_recipient_user_id:
            raise DingTalkDWSError("未配置朱兴福的钉钉 userId，拒绝真实发送")
        if user_id != settings.dingtalk_live_recipient_user_id:
            raise DingTalkDWSError("真实发送收件人不匹配，拒绝外发")

        headers = {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else {}
        try:
            response = httpx.post(
                self._endpoint(),
                json={"user_id": user_id, "text": text, "idempotency_key": idempotency_key},
                headers=headers,
                timeout=45.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.ConnectError as exc:
            raise DingTalkDWSError(
                "Windows 钉钉发送服务未启动或不可达；请运行 "
                "powershell -ExecutionPolicy Bypass -File .\\scripts\\start_collector.ps1 后重试"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DingTalkDWSError(f"Windows DWS 发送器不可用：{str(exc)[:160]}") from exc
        if not payload.get("success"):
            raise DingTalkDWSError(str(payload.get("error") or "DWS 发送失败")[:160])
        return payload.get("result") or {}
