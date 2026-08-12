"""
知识问答 Agent（子图 B）
========================
职责：企业内部制度/流程/话术类问题的知识问答。
rag(检索知识库) → report(口语化知识回答)

由 Supervisor 路由进入；节点函数复用 agent/nodes.py。
- 不绑定任何数据工具、不做经营分析（与经营分析 Agent 完全解耦）
- report_node 对非数据类问题自动使用 KNOWLEDGE_REPORT_PROMPT（口语化模板）
- 知识问答报告不写入经营经验层（report_node 内已处理）
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agent.nodes import rag_node, report_node
from agent.state import AgentState

logger = logging.getLogger(__name__)


def build_kb_agent():
    """知识问答 Agent：rag → report → END"""
    graph = StateGraph(AgentState)

    graph.add_node("rag", rag_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "rag")
    graph.add_edge("rag", "report")
    graph.add_edge("report", END)

    compiled = graph.compile()
    logger.info("知识问答 Agent 子图构建完成")
    return compiled
