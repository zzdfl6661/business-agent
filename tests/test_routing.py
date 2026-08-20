# -*- coding: utf-8 -*-
"""#13-① 意图路由矩阵：resolve_intent / is_data_question / is_market_question。

覆盖：#6 统一路由配置（supervisor 与 report 共用同一判定）、
双列表命中冲突（知识词 vs 数据词）、默认 RAG。
"""
import pytest

from agent.routing import (
    DATA_KEYS,
    MARKET_KEYS,
    is_data_question,
    is_market_question,
    is_sales_ranking_question,
    resolve_intent,
    should_retrieve_operation_knowledge,
)


@pytest.mark.parametrize(
    "question,expected",
    [
        # ---- 数据类（命中 DATA_KEYS）----
        ("分析最近7天1号门店营业额下降原因", "data"),
        ("各门店 GMV 对比", "data"),
        ("智选展位推广 ROI 怎么样", "data"),
        ("最近7天订单量趋势", "data"),
        ("哪些门店排名靠前", "data"),
        ("团建套餐品类表现如何", "data"),
        ("店铺的客流和交易数据", "data"),
        ("最近7天门店销量最多的是哪一家", "data"),
        # ---- 知识类（默认 RAG / 命中 KNOWLEDGE_KEYS）----
        ("我是新员工，每天上班时间是多少", "kb"),
        ("门店晋升需要什么条件", "kb"),
        ("薪资绩效是怎么算的", "kb"),
        ("顾客不满意怎么安抚", "kb"),
        ("公司有多少门店", "kb"),          # 未命中数据词 → 默认知识问答
        ("公司高管有哪些人", "kb"),
        # ---- 冲突用例（#6：知识词优先于数据词）----
        ("报销流程数据在哪里看", "kb"),     # 同时命中"报销"(知识) + "数据"(数据) → kb
        ("绩效考核怎么算", "kb"),          # 命中"绩效" → kb（即使含"算"）
        ("员工培训流程是什么", "kb"),       # 命中"培训/流程" → kb
        # ---- 边界：空串 / None ----
        ("", "kb"),
        (None, "kb"),
    ],
)
def test_resolve_intent(question, expected):
    assert resolve_intent(question) == expected


@pytest.mark.parametrize(
    "question,expected",
    [
        ("分析最近7天1号门店营业额下降原因", True),
        ("报销流程数据在哪里看", False),   # 知识词优先 → 非数据
        ("公司有多少门店", False),
        ("每天上班时间", False),
    ],
)
def test_is_data_question(question, expected):
    assert is_data_question(question) is expected


@pytest.mark.parametrize(
    "question,expected",
    [
        ("各门店综合表现排名怎么样", True),
        ("哪家店客流最好", True),
        ("最近7天营业额下降原因", False),
        ("1号门店订单趋势", False),
    ],
)
def test_is_market_question(question, expected):
    assert is_market_question(question) is expected


def test_routing_keyword_sets():
    """词表自身约束：知识词不应误伤纯数据问题（抽查几个典型数据词）。"""
    assert any(k in "营业额" for k in DATA_KEYS)
    assert "排名" in MARKET_KEYS
    # 知识词集合非空且无空串
    from agent.routing import KNOWLEDGE_KEYS

    assert KNOWLEDGE_KEYS and all(k for k in KNOWLEDGE_KEYS)


def test_sales_ranking_and_factual_data_retrieval_policy():
    question = "最近7天门店销量最多的是哪一家"
    assert is_sales_ranking_question(question)
    assert is_market_question(question)
    assert not should_retrieve_operation_knowledge(question, "data")
    assert should_retrieve_operation_knowledge("营业额下降原因和优化建议", "data")
