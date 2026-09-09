"""
API 鉴权中间件
==============
安全设计（防"任何能访问 8000 端口的人都能调用接口"）：

- 配置 `BIZ_API_TOKEN` 后，所有**非公开路径**（/api/*、/docs、/openapi.json 等）
  必须携带请求头之一：
    Authorization: Bearer <token>
    X-API-Token: <token>
- 未配置 token（开发模式）→ 鉴权关闭、放行全部请求，但启动时与首次请求时输出警告
- 公开路径白名单：/（前端页面）、/static/*（静态资源）、/health（探活）
- token 比较使用 hmac.compare_digest（常数时间，防时序侧信道）
- 每个请求绑定 X-Request-ID；可用 X-Operator-ID 标注共享 token 下的调用方
"""
from __future__ import annotations

import hmac
import json
import logging
import re

from config.request_id import (
    get_request_id,
    new_request_id,
    reset_operator_id,
    reset_request_id,
    set_operator_id,
    set_request_id,
)

logger = logging.getLogger(__name__)

# 公开路径：前端页面 / 静态资源 / 健康检查不需要 token
PUBLIC_EXACT = {"/", "/health"}
PUBLIC_PREFIXES = ("/static",)


class ApiTokenMiddleware:
    """FastAPI/Starlette ASGI 中间件：非公开路径强制 Bearer Token 鉴权。"""

    def __init__(self, app, token: str = ""):
        self.app = app
        self.token = token or ""
        self._warned = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = self._headers(scope)
        request_id = headers.get("x-request-id", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", request_id):
            request_id = new_request_id()
        # 共享 API token 无法证明个人身份；该字段只是已授权调用方提供的稳定审计标识。
        supplied_operator = headers.get("x-operator-id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9@._:-]{1,128}", supplied_operator):
            supplied_operator = ""
        operator_id = supplied_operator or ("api-token-user" if self.token else "development-user")
        request_token = set_request_id(request_id)
        operator_token = set_operator_id(operator_id)
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["operator_id"] = operator_id

        async def send_with_request_id(message):
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in response_headers):
                    response_request_id = get_request_id() or request_id
                    response_headers.append((b"x-request-id", response_request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        path = scope.get("path", "") or ""
        try:
            if self._is_public(path):
                await self.app(scope, receive, send_with_request_id)
                return

            # 未配置 token：开发模式放行，但明确警告
            if not self.token:
                if not self._warned:
                    logger.warning(
                        "BIZ_API_TOKEN 未配置——API 鉴权已禁用！生产环境务必设置，"
                        "否则任何可访问本服务的人都可调用接口（消耗 LLM 额度 / 触发数据采集 / 确认通知草稿）。"
                    )
                    self._warned = True
                await self.app(scope, receive, send_with_request_id)
                return

            provided = self._extract_token(scope)
            if provided and hmac.compare_digest(provided, self.token):
                await self.app(scope, receive, send_with_request_id)
                return

            body = json.dumps(
                {
                    "success": False,
                    "error": "Unauthorized: 缺少或错误的 API Token（请求头需携带 Authorization: Bearer <token>）",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await send_with_request_id({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send_with_request_id({"type": "http.response.body", "body": body})
        finally:
            reset_operator_id(operator_token)
            reset_request_id(request_token)

    @staticmethod
    def _is_public(path: str) -> bool:
        if path in PUBLIC_EXACT:
            return True
        return any(path.startswith(p) for p in PUBLIC_PREFIXES)

    @staticmethod
    def _extract_token(scope) -> str:
        headers = ApiTokenMiddleware._headers(scope)
        auth = headers.get("authorization", "") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (headers.get("x-api-token") or "").strip()

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in scope.get("headers", []) or []:
            headers[key.decode("latin-1").lower()] = value.decode("latin-1")
        return headers
