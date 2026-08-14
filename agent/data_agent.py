"""
经营分析 Agent（子图 A）
========================
职责：数据类问题的完整分析链路。
intent(工具决策) → [tools ⇄ intent 回环] → analysis(确定性 Pandas) → rag(知识) → report(经营诊断)

由 Supervisor 路由进入；节点函数复用 agent/nodes.py。
知识类问题即使误路由进来也安全：intent/analysis 均含知识类短路（不查数据）。
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    analysis_node,
    intent_node,
    rag_node,
    report_node,
    route_after_intent,
    tools_node,
)
from agent.state import AgentState

logger = logging.getLogger(__name__)


def build_data_agent():
    """经营分析 Agent：intent → [tools⇄intent] → analysis → rag → report → END"""
    graph = StateGraph(AgentState)

    graph.add_node("intent", intent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("rag", rag_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "intent")
    graph.add_conditional_edges(
        "intent",
        route_after_intent,
        {"tools": "tools", "analysis": "analysis"},
    )
    graph.add_edge("tools", "intent")   # ReAct 回环
    graph.add_edge("analysis", "rag")
    graph.add_edge("rag", "report")
    graph.add_edge("report", END)

    compiled = graph.compile()
    logger.info("经营分析 Agent 子图构建完成")
    return compiled
