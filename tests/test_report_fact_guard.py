from agent.nodes import _deterministic_report_sections, _enforce_report_facts, _enforce_report_text


def test_stale_online_and_missing_campaign_are_not_misreported():
    state = {
        "analysis_result": {"data": {"data_freshness": {"traffic": {"stale": True}}}},
        "query_result": {
            "sales": {"data": {"summary": {"data_source": "美团经营参谋-线上交易报表"}}},
            "campaign": {"success": False, "error": "只有全局报表"},
        },
    }
    sections = {
        "summary": ["本周线上营业额 5 万元"],
        "factors": ["本周无任何推广活动，缺乏流量抓手"],
    }
    cleaned = _enforce_report_facts(state, sections)
    assert cleaned["summary"] == ["数据周期内线上下单金额 5 万元"]
    assert cleaned["factors"] == ["单店推广报表缺少门店维度，暂不能判断该店投放情况"]

    markdown = "【摘要】\n- 本周线上营业额 5 万元\n- 本周无任何推广活动"
    guarded = _enforce_report_text(state, markdown)
    assert "本周" not in guarded
    assert "线上营业额" not in guarded
    assert "单店推广报表缺少门店维度" in guarded


def test_deterministic_sections_use_calculated_metrics_and_factor_suggestions():
    state = {
        "analysis_result": {"data": {
            "metrics": {"online_order_amount": 1200.0, "order_count": 8, "visit_intention_rate": 1.2},
            "factors": [{"impact": "转化偏低", "evidence": "访问 100 人，意向 1 人", "suggestion": "优化套餐展示"}],
            "data_freshness": {},
        }},
    }
    sections = _deterministic_report_sections(state)
    assert "1,200 元" in sections["summary"][0]
    assert sections["factors"] == ["转化偏低：访问 100 人，意向 1 人"]
    assert sections["actions"] == ["优化套餐展示"]
