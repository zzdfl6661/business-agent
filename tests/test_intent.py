from agent.intent import classify_intent
from config.settings import settings
from agent.nodes import _build_data_tool_plan


def test_data_analysis_and_notify_are_composed(monkeypatch):
    monkeypatch.setattr(settings, "intent_llm_fallback", False)
    result = classify_intent("分析最近7天1号门店营业额下降原因，并通知店长")
    assert result.primary_intent == "data_analysis"
    assert result.requested_actions == ["notify_manager"]


def test_followup_notify_uses_session_analysis(monkeypatch):
    monkeypatch.setattr(settings, "intent_llm_fallback", False)
    result = classify_intent("把刚才的分析结果发给店长")
    assert result.primary_intent == "notify_followup"
    assert result.requested_actions == ["notify_manager"]


def test_knowledge_word_stays_knowledge_without_notification(monkeypatch):
    monkeypatch.setattr(settings, "intent_llm_fallback", False)
    result = classify_intent("报销流程数据在哪里看")
    assert result.primary_intent == "knowledge_qa"


def test_explicit_iso_date_uses_llm_tool_planning_path():
    assert _build_data_tool_plan("查询 2026-08-01 到 2026-08-15 的营业额", 1) is None


def test_common_metric_query_keeps_deterministic_tool_plan():
    plan = _build_data_tool_plan("最近7天1号店营业额和推广ROI", 1)
    assert [step["name"] for step in plan] == ["get_sales_data", "get_campaign_data"]
