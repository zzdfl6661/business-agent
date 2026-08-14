"""
LLM 多 Provider 工厂
====================
统一入口 create_llm()，业务代码只依赖 BaseChatModel，不感知具体 Provider。

- deepseek : ChatDeepSeek（langchain-deepseek 官方包，原生 tool calling）
- openai   : ChatOpenAI
- local    : ChatOpenAI 指向本地 OpenAI 兼容端点（vLLM / Ollama）
- codebuddy: ChatOpenAI 指向 workbuddy2api 本地代理（复用 WorkBuddy 账号额度，原生 tool calling）

#7 优化：按 provider 缓存复用实例（旧实现每次调用都新建，连接/token 开销浪费）
#15 优化：支持运行时切换 provider（POST /api/llm/switch，无需改 .env 重启），
         未显式切换时仍取 settings.llm_provider（.env 默认值）。

真实数据模式：不提供任何 Mock 降级——未配置 API Key 时抛出明确错误，
提示配置对应通道（BIZ_LLM_PROVIDER + Key / workbuddy2api 服务），避免"假报告"误导。
"""
from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from config.settings import settings

logger = logging.getLogger(__name__)

# 支持的 provider 列表（用于 /api/llm/switch 校验与前端下拉）
SUPPORTED_PROVIDERS = ("deepseek", "openai", "local", "codebuddy")

# 实例缓存：provider → BaseChatModel（按 provider 复用，避免每次调用新建）
_instances: dict[str, BaseChatModel] = {}
# 运行时激活 provider（#15）：None = 跟随 settings.llm_provider（.env）
_active_provider: str | None = None


def get_active_provider() -> str:
    """当前生效的 provider（运行时切换优先，否则 .env 默认）。"""
    return (_active_provider or settings.llm_provider).strip().lower()


def set_active_provider(provider: str | None) -> str:
    """运行时切换 LLM provider（内存级，无需重启；#15）。

    - 校验 provider 合法性并尝试创建实例（未配置 Key 立即抛 ValueError）
    - 切换后该 provider 的实例按需重建（换 base_url/model 时先清缓存）
    """
    global _active_provider
    if provider is None:
        _active_provider = None
        return get_active_provider()
    p = provider.strip().lower()
    if p not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"未知 LLM provider={provider!r}（可选：{' / '.join(SUPPORTED_PROVIDERS)}）"
        )
    # 立即校验（未配置 Key 会抛 ValueError，前端可显示具体错误）
    create_llm(p)
    _active_provider = p
    logger.info("LLM provider 运行时切换：%s", p)
    return p


def list_providers() -> dict:
    """当前 provider 状态（/api/llm/providers）。"""
    return {
        "supported": list(SUPPORTED_PROVIDERS),
        "active": get_active_provider(),
        "config": {
            "deepseek": {"model": settings.deepseek_model, "configured": bool(settings.deepseek_api_key)},
            "openai": {"model": settings.openai_model, "configured": bool(settings.openai_api_key)},
            "local": {"base_url": settings.local_base_url, "model": settings.local_model, "configured": True},
            "codebuddy": {"base_url": settings.codebuddy_base_url, "model": settings.codebuddy_model, "configured": True},
        },
    }


def create_llm(provider: str | None = None) -> BaseChatModel:
    """创建（或复用缓存）LLM 实例。provider 缺省时取运行时激活 / settings.llm_provider。

    未配置对应通道的 API Key / 服务时抛 ValueError（真实链路，无 Mock 降级）。
    """
    provider = (provider or get_active_provider()).strip().lower()
    if provider in _instances:
        return _instances[provider]
    llm = _build_llm(provider)
    _instances[provider] = llm
    return llm


def reset_llm_cache() -> None:
    """测试 / 切换 base_url 后清空实例缓存。"""
    _instances.clear()


def _http_client():
    """统一 httpx 客户端：显式 trust_env=False（忽略 HTTP(S)_PROXY 环境代理）。

    踩坑：开发机环境注入 HTTP_PROXY=http://127.0.0.1:10090（WorkBuddy 本地代理），
    httpx 默认 trust_env=True 会把 LLM 请求走该代理——代理间歇不可用时出现
    "Connection error"（openai.APIConnectionError），表现为对话偶发失败。
    直连可避免：LLM 通道（deepseek/codebuddy/local）均直接可达。
    """
    import httpx

    return httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=True,
    )


def _build_llm(provider: str) -> BaseChatModel:
    """构建新 LLM 实例（不查缓存）。"""
    if provider == "deepseek":
        if not settings.deepseek_api_key:
            raise ValueError("BIZ_LLM_PROVIDER=deepseek 但未配置 BIZ_DEEPSEEK_API_KEY，请填写 Key")
        from langchain_deepseek import ChatDeepSeek

        logger.info("LLM provider=deepseek, model=%s", settings.deepseek_model)
        return ChatDeepSeek(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            temperature=settings.llm_temperature,
            http_client=_http_client(),  # 绕过环境代理，防间歇性 Connection error
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("BIZ_LLM_PROVIDER=openai 但未配置 BIZ_OPENAI_API_KEY，请填写 Key")
        from langchain_openai import ChatOpenAI

        logger.info("LLM provider=openai, model=%s", settings.openai_model)
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=settings.llm_temperature,
            http_client=_http_client(),
        )

    if provider == "local":
        from langchain_openai import ChatOpenAI

        logger.info("LLM provider=local, base_url=%s model=%s", settings.local_base_url, settings.local_model)
        return ChatOpenAI(
            model=settings.local_model,
            api_key="sk-local",  # 本地端点通常不校验
            base_url=settings.local_base_url,
            temperature=settings.llm_temperature,
            http_client=_http_client(),
        )

    if provider == "codebuddy":
        from langchain_openai import ChatOpenAI

        logger.info("LLM provider=codebuddy, base_url=%s model=%s", settings.codebuddy_base_url, settings.codebuddy_model)
        return ChatOpenAI(
            model=settings.codebuddy_model,
            api_key=settings.codebuddy_api_key or "codebuddy-local",  # workbuddy2api 默认不校验 Key
            base_url=settings.codebuddy_base_url,
            temperature=settings.llm_temperature,
            http_client=_http_client(),
        )

    raise ValueError(
        f"未知 LLM provider={provider!r}（可选：{' / '.join(SUPPORTED_PROVIDERS)}）。"
        "请检查 .env 的 BIZ_LLM_PROVIDER 配置。"
    )
