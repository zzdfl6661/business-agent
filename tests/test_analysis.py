# -*- coding: utf-8 -*-
"""#13-② analysis_business_data 指标计算边界：除零 / 空窗口 / 无数据。

指标计算是 Pandas 确定性逻辑，任何边界都不能抛异常（LLM 依赖其结果生成报告）。
"""
import pytest

from tools.analysis_tool import analysis_business_data


def _sales(summary=None, daily=None, prev=None, category=None, top_products=None):
    return {
        "success": True,
        "data": {
            "summary": summary or {},
            "daily": daily or [],
            "prev_daily": prev or [],
            "category_breakdown": category or [],
            "top_products": top_products or [],
        },
        "error": None,
    }


def test_empty_inputs():
    """全空输入：不抛异常，指标为 0/None。"""
    r = analysis_business_data.invoke({"sales_data": {}, "campaign_data": {}})
    assert r["success"] is True
    m = r["data"]["metrics"]
    assert m["gmv"] == 0.0
    assert m["order_count"] == 0
    assert m["gmv_change_pct"] is None
    assert r["data"]["factors"][0]["type"] == "normal"


def test_zero_prev_no_divzero():
    """上期全 0（prev_gmv=0）：环比应为 None，不抛除零异常。"""
    sales = _sales(
        summary={"gmv": 1000, "order_count": 10, "avg_order_value": 100, "prev_gmv": 0, "prev_order_count": 0},
    )
    r = analysis_business_data.invoke({"sales_data": sales})
    assert r["success"] is True
    assert r["data"]["metrics"]["gmv_change_pct"] is None
    assert r["data"]["metrics"]["order_change_pct"] is None


def test_zero_orders_no_divzero():
    """本期 0 订单（除零客单价）：avg_order_value=0，不抛异常。"""
    sales = _sales(
        summary={"gmv": 0, "order_count": 0, "prev_gmv": 1000, "prev_order_count": 10},
    )
    r = analysis_business_data.invoke({"sales_data": sales})
    assert r["success"] is True
    assert r["data"]["metrics"]["avg_order_value"] == 0.0


def test_daily_window_calc():
    """日粒度窗口：GMV/订单/环比正确聚合。"""
    sales = _sales(
        daily=[{"date": "2026-08-01", "gmv": 100, "orders": 2}] * 3,
        prev=[{"date": "2026-07-25", "gmv": 200, "orders": 4}] * 3,
    )
    r = analysis_business_data.invoke({"sales_data": sales})
    m = r["data"]["metrics"]
    assert m["gmv"] == 300.0
    assert m["order_count"] == 6
    assert m["gmv_change_pct"] == -50.0  # 300 vs 600


def test_campaign_empty_and_roi():
    """推广数据：无 campaigns 时 ROI 为 None；有低 ROI 时出 roi_low 异常。"""
    r = analysis_business_data.invoke({
        "sales_data": _sales(),
        "campaign_data": {"success": True, "data": {"campaigns": [], "total_spent": 0}},
    })
    assert r["data"]["metrics"]["campaign_roi_avg"] is None

    camp = {"success": True, "data": {"campaigns": [
        {"name": "A", "spent": 1000, "clicks": 500, "conversions": 10, "roi": 1.2},
    ], "total_spent": 1000}}
    r2 = analysis_business_data.invoke({"sales_data": _sales(), "campaign_data": camp})
    assert r2["data"]["metrics"]["campaign_roi_avg"] == 1.2
    assert any(a["type"] == "roi_low" for a in r2["data"]["anomalies"])
