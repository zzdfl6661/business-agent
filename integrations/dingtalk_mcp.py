"""钉钉官方 MCP 客户端边界。

第一阶段默认 dry-run，绝不建立外部连接或调用发送工具。live 模式仅在全部
配置齐备后才允许调用官方 dingtalk-mcp stdio 服务或 Streamable HTTP MCP Server。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from config.settings import settings


class DingTalkMCPError(RuntimeError):
    pass


class DingTalkMCPClient:
    """将钉钉 MCP 的具体工具名收敛到两个业务能力。"""

    def __init__(self, transport: Any | None = None):
        # 仅测试可注入内存 MCPServer；生产走官方 stdio 或已部署的 HTTP 网关。
        self._transport = transport

    def _validate_live_config(self, *, require_recipient: bool = True) -> None:
        if not settings.dingtalk_enabled or settings.dingtalk_mode != "live":
            raise DingTalkMCPError("钉钉真实发送未启用（需 BIZ_DINGTALK_ENABLED=true 且 mode=live）")
        if self._transport is not None:
            missing = [name for name, value in {
                "BIZ_DINGTALK_MCP_SEARCH_USER_TOOL": settings.dingtalk_mcp_search_user_tool,
                "BIZ_DINGTALK_MCP_SEND_TEXT_TOOL": settings.dingtalk_mcp_send_text_tool,
            }.items() if not value]
            if missing:
                raise DingTalkMCPError(f"钉钉 MCP 配置不完整：{', '.join(missing)}")
            return
        common = {
            "BIZ_DINGTALK_MCP_SEARCH_USER_TOOL": settings.dingtalk_mcp_search_user_tool,
            "BIZ_DINGTALK_MCP_SEND_TEXT_TOOL": settings.dingtalk_mcp_send_text_tool,
        }
        if require_recipient:
            common["BIZ_DINGTALK_LIVE_RECIPIENT_USER_ID"] = settings.dingtalk_live_recipient_user_id
        if settings.dingtalk_mcp_transport == "stdio":
            required = {
                "BIZ_DINGTALK_CLIENT_ID": settings.dingtalk_client_id,
                "BIZ_DINGTALK_CLIENT_SECRET": settings.dingtalk_client_secret,
                "BIZ_DINGTALK_ROBOT_CODE": settings.dingtalk_robot_code,
                **common,
            }
        elif settings.dingtalk_mcp_transport == "streamable_http":
            required = {
                "BIZ_DINGTALK_MCP_URL": settings.dingtalk_mcp_url,
                "BIZ_DINGTALK_MCP_AUTH_TOKEN": settings.dingtalk_mcp_auth_token,
                **common,
            }
        else:
            raise DingTalkMCPError("BIZ_DINGTALK_MCP_TRANSPORT 仅支持 stdio 或 streamable_http")
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise DingTalkMCPError(f"钉钉 MCP 配置不完整：{', '.join(missing)}")

    @asynccontextmanager
    async def _connected_client(self):
        """建立一次官方 stdio 或带认证头的 Streamable HTTP MCP 会话。"""
        try:
            from mcp import Client
            if self._transport is not None:
                async with Client(self._transport) as client:
                    yield client
                return

            if settings.dingtalk_mcp_transport == "stdio":
                from mcp.client.stdio import StdioServerParameters

                params = StdioServerParameters(
                    # Docker 镜像已固定安装这个版本，不在真实发送时临时下载 npm 包。
                    command="dingtalk-mcp",
                    args=[],
                    env={
                    "DINGTALK_Client_ID": settings.dingtalk_client_id,
                    "DINGTALK_Client_Secret": settings.dingtalk_client_secret,
                    "ACTIVE_PROFILES": "dingtalk-contacts,dingtalk-robot-send-message",
                    "ROBOT_CODE": settings.dingtalk_robot_code,
                    },
                )
                async with Client(params) as client:
                    yield client
                return

            import httpx
            from mcp.client.streamable_http import streamable_http_client

            headers = {"Authorization": f"Bearer {settings.dingtalk_mcp_auth_token}"}
            async with httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(30.0, read=300.0),
                follow_redirects=True,
            ) as http_client:
                transport = streamable_http_client(settings.dingtalk_mcp_url, http_client=http_client)
                async with Client(transport) as client:
                    yield client
        except ImportError as exc:
            raise DingTalkMCPError("未安装 mcp 依赖，请安装 requirements.txt") from exc

    async def describe_capabilities(self) -> dict[str, Any]:
        """live 模式执行工具发现；dry-run/disabled 永不联网。"""
        if not settings.dingtalk_enabled or settings.dingtalk_mode != "live":
            return {"status": "disabled", "tools": []}
        # 先允许发现工具，便于首次配置时查询朱兴福的 userId。
        self._validate_live_config(require_recipient=False)
        try:
            async with self._connected_client() as client:
                result = await client.list_tools()
                names = [tool.name for tool in result.tools]
        except DingTalkMCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DingTalkMCPError(f"钉钉 MCP 能力发现失败：{str(exc)[:160]}") from exc
        required = {settings.dingtalk_mcp_search_user_tool, settings.dingtalk_mcp_send_text_tool}
        missing = sorted(required - set(names))
        return {"status": "ready" if not missing else "unavailable", "tools": names, "missing": missing}

    async def resolve_user_by_mobile(self, mobile: str) -> dict:
        # 通讯录查询用于首次取得固定收件人的 userId，此时尚不要求已配置该 ID。
        self._validate_live_config(require_recipient=False)
        try:
            async with self._connected_client() as client:
                result = await client.call_tool(
                    settings.dingtalk_mcp_search_user_tool, {"mobile": mobile}
                )
        except DingTalkMCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DingTalkMCPError(f"钉钉通讯录查询失败：{str(exc)[:160]}") from exc
        if getattr(result, "is_error", False):
            raise DingTalkMCPError("钉钉通讯录 MCP 返回错误")
        return self._result_payload(result)

    async def send_text(self, user_id: str, text: str) -> dict:
        self._validate_live_config()
        try:
            async with self._connected_client() as client:
                arguments = {"user_id": user_id, "text": text}
                if (
                    settings.dingtalk_mcp_transport == "stdio"
                    and settings.dingtalk_mcp_send_text_tool == "batchSendMessageToUsersByRobot"
                ):
                    arguments = {
                        "userIds": [user_id],
                        "msgKey": "sampleMarkdown",
                        "msgParam": {"title": "经营提醒", "text": text},
                    }
                result = await client.call_tool(
                    settings.dingtalk_mcp_send_text_tool, arguments,
                )
        except ImportError as exc:
            raise DingTalkMCPError("未安装 mcp 依赖") from exc
        except Exception as exc:  # noqa: BLE001
            raise DingTalkMCPError(f"钉钉消息发送失败：{str(exc)[:160]}") from exc
        if getattr(result, "is_error", False):
            raise DingTalkMCPError("钉钉机器人消息 MCP 返回错误")
        return self._result_payload(result)

    @staticmethod
    def _result_payload(result: Any) -> dict:
        """兼容官方 MCP 工具的 structured content 和 JSON 文本结果。"""
        if getattr(result, "structured_content", None):
            return result.structured_content
        content = getattr(result, "content", [])
        for block in content:
            raw = getattr(block, "text", None)
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return {"content": str(content)}

    def send_text_sync(self, user_id: str, text: str) -> dict:
        """FastAPI 同步确认接口使用；仅 live 模式可进入。"""
        return asyncio.run(self.send_text(user_id, text))

    def describe_capabilities_sync(self) -> dict[str, Any]:
        return asyncio.run(self.describe_capabilities())

    def resolve_user_by_mobile_sync(self, mobile: str) -> dict:
        return asyncio.run(self.resolve_user_by_mobile(mobile))
