# -*- coding: utf-8 -*-
"""数据查询规划、上下文压缩与 RAG 降级策略的无网络回归测试。"""
import json

from agent.nodes import _build_data_tool_plan, _build_report_input, _merge_stream_text


def test_sales_question_uses_one_deterministic_query():
    plan = _build_data_tool_plan("分析 2 号店最近营业额和订单趋势", 2)
    assert plan == [{"name": "get_sales_data", "args": {"store_id": 2, "days": 7}}]


def test_deterministic_plan_keeps_relative_date_window():
    plan = _build_data_tool_plan("分析 2 号店最近30天营业额", 2)
    assert plan == [{"name": "get_sales_data", "args": {"store_id": 2, "days": 30}}]


def test_explicit_dates_fall_back_to_llm_parameter_extraction():
    assert _build_data_tool_plan("分析 2026-08-01 到 2026-08-07 的营业额", 2) is None


def test_campaign_question_does_not_query_sales_unnecessarily():
    plan = _build_data_tool_plan("本月推广 ROI 和花费怎么样", 3)
    assert plan == [{"name": "get_campaign_data", "args": {"store_id": 3, "days": 30}}]


def test_sales_ranking_uses_mysql_ranking_tool_not_knowledge_base():
    plan = _build_data_tool_plan("最近7天门店销量最多的是哪一家", None)
    assert plan == [{"name": "get_store_sales_ranking", "args": {"days": 7, "metric": "sales_volume", "top_n": 10}}]


def test_cumulative_stream_chunks_do_not_duplicate_report():
    text = _merge_stream_text("", "第一段")
    text = _merge_stream_text(text, "第一段第二段")
    text = _merge_stream_text(text, "第一段第二段")
    assert text == "第一段第二段"


def test_budget_change_keeps_llm_planning_for_safe_execution_plan():
    assert _build_data_tool_plan("把 2 号店推广预算调整到 5000", 2) is None


def test_report_input_trims_sales_and_campaign_lists():
    state = {
        "user_question": "分析营业额",
        "intent_type": "data",
        "query_result": {
            "sales": {"data": {
                "summary": {"gmv": 100},
                "daily": [{"date": str(i), "gmv": i} for i in range(10)],
                "prev_daily": [{"date": str(i), "gmv": i} for i in range(10)],
                "top_products": [{"name": str(i)} for i in range(5)],
                "category_breakdown": [{"name": str(i)} for i in range(8)],
            }},
            "campaign": {"data": {"campaigns": [{"name": str(i)} for i in range(12)]}},
        },
        "analysis_result": {"data": {"metrics": {"gmv": 100}}},
        "retrieval_docs": [],
    }
    payload = json.loads(_build_report_input(state))
    sales = payload["query_result"]["sales"]
    assert "prev_daily" not in sales
    assert len(sales["daily"]) == 7
    assert len(sales["top_products"]) == 3
    assert len(sales["category_breakdown"]) == 5
    assert len(payload["query_result"]["campaign"]["campaigns"]) == 3
