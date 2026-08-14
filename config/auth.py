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
"""
from __future__ import annotations

import hmac
import json
import logging

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

        path = scope.get("path", "") or ""
        if self._is_public(path):
            await self.app(scope, receive, send)
            return

        # 未配置 token：开发模式放行，但明确警告
        if not self.token:
            if not self._warned:
                logger.warning(
                    "BIZ_API_TOKEN 未配置——API 鉴权已禁用！生产环境务必设置，"
                    "否则任何可访问本服务的人都可调用接口（消耗 LLM 额度 / 触发数据采集 / 确认执行预算修改）。"
                )
                self._warned = True
            await self.app(scope, receive, send)
            return

        provided = self._extract_token(scope)
        if provided and hmac.compare_digest(provided, self.token):
            await self.app(scope, receive, send)
            return

        body = json.dumps(
            {
                "success": False,
                "error": "Unauthorized: 缺少或错误的 API Token（请求头需携带 Authorization: Bearer <token>）",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _is_public(path: str) -> bool:
        if path in PUBLIC_EXACT:
            return True
        return any(path.startswith(p) for p in PUBLIC_PREFIXES)

    @staticmethod
    def _extract_token(scope) -> str:
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []) or []:
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        auth = headers.get("authorization", "") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (headers.get("x-api-token") or "").strip()
