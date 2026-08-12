"""
LLM 多 Provider 工厂
====================
统一入口 create_llm()，业务代码只依赖 BaseChatModel，不感知具体 Provider。

- deepseek : ChatDeepSeek（langchain-deepseek 官方包，原生 tool calling）
- openai   : ChatOpenAI
- local    : ChatOpenAI 指向本地 OpenAI 兼容端点（vLLM / Ollama）
- codebuddy: ChatOpenAI 指向 workbuddy2api 本地代理（复用 WorkBuddy 账号额度，原生 tool calling）
- mock     : MockLLM（无 Key 时自动降级，保证服务可启动、链路可演示）

关键约束：
- DeepSeek 模型名通过 BIZ_DEEPSEEK_MODEL 配置（默认 deepseek-v4-flash，兼容 deepseek-chat）
- DeepSeek / OpenAI 未配置 Key 时自动回退 MockLLM
- codebuddy 通道无需 Key（workbuddy2api 复用本机登录态），通过 BIZ_CODEBUDDY_BASE_URL / BIZ_CODEBUDDY_MODEL 配置
"""
from __future__ import annotations

import json
import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from config.settings import settings

logger = logging.getLogger(__name__)


def _render_mock_report(question: str, metrics: dict, factors: list, docs: list) -> str:
    """基于真实 Pandas 计算结果渲染占位报告，验证链路的同时展示真实数值。"""
    gmv_change = metrics.get("gmv_change_pct")
    order_change = metrics.get("order_change_pct")
    roi = metrics.get("campaign_roi_avg")
    lines = [
        "## 经营诊断报告（MockLLM 占位输出）",
        "",
        "> ⚠️ 当前为骨架阶段：LLM 为 Mock，但指标由 Pandas 真实计算。配置 `BIZ_DEEPSEEK_API_KEY` 且 `BIZ_DATA_MODE=real` 后输出真实报告。",
        "",
        f"**分析问题**：{question}",
        "",
        "### 一、结论摘要（模拟）",
    ]
    if gmv_change is not None and gmv_change <= -10:
        lines.append(f"- 营业额环比变化 {gmv_change}%，存在明显下滑，需重点排查（订单量环比 {order_change}%）")
    else:
        lines.append("- 营业额环比变化 %s，运营状态平稳" % (f"{gmv_change}%" if gmv_change is not None else "未知"))
    if roi is not None:
        lines.append(f"- 推广平均 ROI {roi}，预算花超风险需关注" if roi < 1.5 else f"- 推广平均 ROI {roi}，处于健康区间")
    lines += ["", "### 二、核心指标变化", ""]
    for k, v in metrics.items():
        if v is not None:
            lines.append(f"- **{k}**：{v}")
    lines += ["", "### 三、异常原因归因（Pandas 计算）", ""]
    for f in factors:
        lines.append(f"- **{f.get('type')}**：{f.get('impact')}")
        if f.get("evidence"):
            lines.append(f"  - 证据：{f.get('evidence')}")
    lines += ["", "### 四、知识库依据（内置示例）", ""]
    for d in docs[:3]:
        src = (d.get("metadata") or {}).get("source") if isinstance(d, dict) else ""
        content = d.get("content", "")[:120] if isinstance(d, dict) else str(d)[:120]
        lines.append(f"- [{src or '示例知识'}] {content}…")
    lines += [
        "",
        "### 五、风险提示与下一步",
        "- 本报告由 Mock 链路生成，仅用于骨架验证；接入真实 LLM 与数据后自动升级",
        "- 如需自动调整推广预算，可调用 update_campaign_budget（需用户授权 confirm=True）",
    ]
    return "\n".join(lines)


class MockLLM(BaseChatModel):
    """
    骨架阶段的兜底模型：不访问任何外部服务。
    - bind_tools 直接返回自身（不产出 tool_calls，图走向确定性分析链路）
    - _generate 返回带占位标注的 markdown 报告，便于验证链路完整性
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_name: str = "mock-llm"
    temperature: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "mock-llm"

    def bind_tools(self, tools, **kwargs) -> "MockLLM":
        """Mock 模式不产出工具调用，直接返回自身。"""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        content = self._mock_reply(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @staticmethod
    def _mock_reply(messages: list[BaseMessage]) -> str:
        last = next((m for m in reversed(messages) if getattr(m, "type", "") == "human"), None)
        content = last.content if last else ""
        if not isinstance(content, str):
            content = str(content)

        # 报告节点输入为 JSON（含 analysis_result / retrieval_docs）→ 渲染结构化占位报告
        try:
            data = json.loads(content)
            question = data.get("user_question", "")
            analysis = (data.get("analysis_result") or {}).get("data", {}) or {}
            metrics = analysis.get("metrics", {}) or {}
            factors = analysis.get("factors", []) or []
            docs = data.get("retrieval_docs", []) or []
            return _render_mock_report(question, metrics, factors, docs)
        except (TypeError, json.JSONDecodeError):
            pass

        # 非报告节点（如 intent）：返回简短说明
        if len(content) > 200:
            content = content[:200] + "…"
        return (
            "## 经营诊断报告（MockLLM 占位输出）\n\n"
            "> 当前未配置 LLM API Key 或 data_mode=mock，以下为骨架阶段占位报告，用于验证链路完整性。\n\n"
            f"**原始问题**：{content}\n\n"
            "**链路状态**：意图分析 ✅ → 工具调用(模拟) ✅ → 数据分析(真实计算) ✅ → 知识检索(示例) ✅ → 报告生成 ✅\n\n"
            "**下一步**：在 `backend/.env` 配置 `BIZ_DEEPSEEK_API_KEY` 并设置 `BIZ_DATA_MODE=real`，\n"
            "运行 `python -m scripts.seed` 灌入业务数据后，即可获得真实经营诊断报告。"
        )


def create_llm(provider: str | None = None) -> BaseChatModel:
    """创建 LLM 实例。provider 缺省时取 settings.llm_provider。"""
    provider = (provider or settings.llm_provider).strip().lower()

    if provider == "deepseek" and settings.deepseek_api_key:
        from langchain_deepseek import ChatDeepSeek

        logger.info("LLM provider=deepseek, model=%s", settings.deepseek_model)
        return ChatDeepSeek(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            temperature=settings.llm_temperature,
        )

    if provider == "openai" and settings.openai_api_key:
        from langchain_openai import ChatOpenAI

        logger.info("LLM provider=openai, model=%s", settings.openai_model)
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=settings.llm_temperature,
        )

    if provider == "local":
        from langchain_openai import ChatOpenAI

        logger.info("LLM provider=local, base_url=%s model=%s", settings.local_base_url, settings.local_model)
        return ChatOpenAI(
            model=settings.local_model,
            api_key="sk-local",  # 本地端点通常不校验
            base_url=settings.local_base_url,
            temperature=settings.llm_temperature,
        )

    if provider == "codebuddy":
        from langchain_openai import ChatOpenAI

        logger.info("LLM provider=codebuddy, base_url=%s model=%s", settings.codebuddy_base_url, settings.codebuddy_model)
        return ChatOpenAI(
            model=settings.codebuddy_model,
            api_key=settings.codebuddy_api_key or "codebuddy-local",  # workbuddy2api 默认不校验 Key
            base_url=settings.codebuddy_base_url,
            temperature=settings.llm_temperature,
        )

    if provider in ("deepseek", "openai") and settings.mock_llm_active:
        logger.warning("provider=%s 未配置 API Key，自动降级为 MockLLM", provider)

    logger.info("LLM provider=mock")
    return MockLLM(temperature=settings.llm_temperature)
