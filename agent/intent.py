"""复合意图识别：规则高精度优先，LLM 仅用于低置信歧义兜底。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.routing import DATA_KEYS, KNOWLEDGE_KEYS, resolve_intent
from config.settings import settings

NOTIFY_WORDS = ("通知", "发送", "同步", "告知", "发给", "转发")
MANAGER_WORDS = ("店长", "门店负责人", "负责人")
FOLLOWUP_WORDS = ("刚才", "上次", "刚刚", "上一条", "刚才的", "刚才结果")


class IntentDecision(BaseModel):
    primary_intent: str = Field("knowledge_qa", description="data_analysis / knowledge_qa / notify_followup")
    requested_actions: list[str] = Field(default_factory=list)
    store_refs: list[str] = Field(default_factory=list)
    time_range: str = ""
    metrics: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    needs_clarification: bool = False
    clarification_reason: str = ""


def _has_any(text: str, values: tuple[str, ...] | list[str]) -> bool:
    return any(value in text for value in values)


def _rule_decision(question: str) -> IntentDecision:
    q = (question or "").lower()
    wants_notify = _has_any(q, NOTIFY_WORDS) and _has_any(q, MANAGER_WORDS)
    is_followup = wants_notify and _has_any(q, FOLLOWUP_WORDS)
    data_hit = _has_any(q, DATA_KEYS)
    knowledge_hit = _has_any(q, KNOWLEDGE_KEYS)
    metrics = [key for key in DATA_KEYS if key in q][:6]

    if is_followup:
        return IntentDecision(
            primary_intent="notify_followup", requested_actions=["notify_manager"],
            metrics=metrics, confidence=0.98,
        )
    if wants_notify and data_hit and not knowledge_hit:
        return IntentDecision(
            primary_intent="data_analysis", requested_actions=["notify_manager"],
            metrics=metrics, confidence=0.96,
        )
    if wants_notify and not data_hit:
        return IntentDecision(
            primary_intent="notify_followup", requested_actions=["notify_manager"],
            metrics=metrics, confidence=0.82,
        )
    resolved = resolve_intent(q)
    return IntentDecision(
        primary_intent="data_analysis" if resolved == "data" else "knowledge_qa",
        metrics=metrics,
        confidence=0.92 if (data_hit or knowledge_hit) else 0.60,
    )


def classify_intent(question: str) -> IntentDecision:
    """返回可组合意图；规则处理高确定性场景，冲突/未知表达交给受限 LLM 分类。"""
    decision = _rule_decision(question)
    q = (question or "").lower()
    data_hit = _has_any(q, DATA_KEYS)
    knowledge_hit = _has_any(q, KNOWLEDGE_KEYS)
    # 两类词同时命中时，规则仍给出知识优先的安全回退，但交给分类器复核，避免
    # “绩效数据趋势”一类复合问题被静默地误路由。
    ambiguous = decision.confidence < 0.8 or (data_hit and knowledge_hit)
    if not settings.intent_llm_fallback or not ambiguous or not q.strip():
        return decision
    try:
        from config.llm_factory import create_llm

        llm = create_llm().with_structured_output(IntentDecision)
        result = llm.invoke(
            "判断用户请求属于经营数据分析、内部知识问答，还是基于刚才分析通知店长。"
            "通知仅在用户明确要求时加入 requested_actions=['notify_manager']。"
            f"用户问题：{question}"
        )
        parsed = result if isinstance(result, IntentDecision) else IntentDecision.model_validate(result)
        if parsed.primary_intent in {"data_analysis", "knowledge_qa", "notify_followup"}:
            return parsed
    except Exception:  # noqa: BLE001
        pass
    return decision
