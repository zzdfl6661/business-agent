"""
Supervisor 主图（多 Agent 路由）
================================
用户问题 → Supervisor（确定性路由）→ 分发到两个独立子 Agent：

    ┌─────────────┐  命中 DATA_KEYS（营业额/订单/环比/推广/排名…）
    │ supervisor  ├────────────────────────────► 经营分析 Agent（data_agent 子图 A）
    └─────┬───────┘   完整数据链路：intent→tools⇄intent→analysis→rag→report
          │
          └ 未命中（制度/手册/流程/话术…）───► 知识问答 Agent（kb_agent 子图 B）
                                                 纯 RAG：rag→report（口语化知识回答）

- 路由策略（默认 RAG）：数据类问题才走工具查询，其余默认知识问答
- 子图作为节点挂载：LangGraph 1.x 原生 subgraph，各自独立的 StateGraph 与状态传递
- 模块级单例 agent，供 API 层复用（入口 state 结构不变：user_question + messages）
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agent.data_agent import build_data_agent
from agent.kb_agent import build_kb_agent
from agent.routing import resolve_intent
from agent.state import AgentState

logger = logging.getLogger(__name__)


def supervisor_node(state: dict) -> dict:
    """确定性路由：命中数据类关键词 → 经营分析 Agent；否则 → 知识问答 Agent。

    #6：统一 resolve_intent 判定（与 report_node 同源，消除判定不一致）；
    #6：顺带解析门店名 → store_id（"XX店营业额"不再让 LLM 猜 store_id=1）。
    """
    question = state.get("user_question", "") or ""
    intent_type = resolve_intent(question)
    update: dict = {"intent_type": intent_type}
    # 门店解析（仅 data 链路需要；kb 链路无需 store_id）
    if intent_type == "data":
        try:
            from tools.store_resolver import resolve_store_id

            store_id = resolve_store_id(question)
            if store_id:
                update["store_id"] = store_id
                logger.info("Supervisor 门店解析：store_id=%s（%s）", store_id, question[:30])
        except Exception as exc:  # noqa: BLE001 解析失败不影响路由
            logger.warning("门店解析失败：%s", exc)
    logger.info("Supervisor 路由：%s → %s Agent | %s", intent_type, intent_type, question[:50])
    return update


def route_after_supervisor(state: dict) -> str:
    return state.get("intent_type", "kb")


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("supervisor", supervisor_node)
    graph.add_node("data_agent", build_data_agent())
    graph.add_node("kb_agent", build_kb_agent())

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {"data": "data_agent", "kb": "kb_agent"},
    )
    graph.add_edge("data_agent", END)
    graph.add_edge("kb_agent", END)

    compiled = graph.compile()
    logger.info("Supervisor 多 Agent 图构建完成：route → data_agent(经营分析) / kb_agent(知识问答)")
    return compiled


agent = build_graph()
