"""顶层可组合编排图。

经营分析和知识问答仍是独立子图；通知子图只在用户明确要求时作为经营分析的下游执行，
并且只创建待审批草稿，绝不由 LLM 直接外发。
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agent.data_agent import build_data_agent
from agent.intent import classify_intent
from agent.kb_agent import build_kb_agent
from agent.notification_agent import build_notification_agent
from agent.state import AgentState

logger = logging.getLogger(__name__)


def supervisor_node(state: dict) -> dict:
    question = state.get("user_question", "") or ""
    decision = classify_intent(question)
    update: dict = {
        "intent_decision": decision.model_dump(),
        "requested_actions": decision.requested_actions,
    }
    if decision.primary_intent == "data_analysis":
        update["intent_type"] = "data"
        try:
            from tools.store_resolver import resolve_store_id

            store_id = resolve_store_id(question)
            if store_id:
                update["store_id"] = store_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("门店解析失败：%s", exc)
        cross_store = any(k in question for k in ("排名", "排行", "哪家", "所有门店", "全部门店", "各门店", "整体"))
        if not update.get("store_id") and not cross_store:
            update["clarification"] = "经营分析需要明确目标门店，请补充门店名称、常用简称或门店编号。"
        # 通知必须精确到单店，禁止继承旧链路的 store_id=1 默认值。
        if "notify_manager" in decision.requested_actions and not update.get("store_id"):
            update["clarification"] = "需要通知店长时必须明确目标门店，请补充门店名称或门店编号。"
    elif decision.primary_intent == "notify_followup":
        update["intent_type"] = "notify_followup"
    else:
        update["intent_type"] = "kb"
    logger.info("Supervisor 复合路由：%s → %s actions=%s", question[:50], update["intent_type"], decision.requested_actions)
    return update


def route_after_supervisor(state: dict) -> str:
    if state.get("clarification"):
        return "clarify"
    return {"data": "data", "kb": "kb", "notify_followup": "followup"}.get(
        state.get("intent_type"), "clarify"
    )


def persist_analysis_node(state: dict) -> dict:
    try:
        from database.mysql import get_session_factory
        from services.analysis_runs import persist_analysis_run

        with get_session_factory()() as session:
            return persist_analysis_run(state, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存分析快照失败：%s", exc)
        if "notify_manager" in (state.get("requested_actions") or []):
            return {"clarification": "经营分析快照保存失败，无法生成可追溯的通知草稿，请稍后重试。"}
        return {}


def route_after_persist_analysis(state: dict) -> str:
    if state.get("clarification"):
        return "clarify"
    return "notify" if "notify_manager" in (state.get("requested_actions") or []) else "end"


def load_last_analysis_node(state: dict) -> dict:
    try:
        from database.mysql import get_session_factory
        from services.analysis_runs import load_latest_session_analysis

        with get_session_factory()() as session:
            run = load_latest_session_analysis(state.get("session_id"), session)
        if not run:
            return {"clarification": "当前会话没有可复用的经营分析，请先完成一次门店分析后再通知店长。"}
        return {
            "intent_type": "data",
            "analysis_run_id": run["analysis_run_id"],
            "analysis_run": run,
            "store_id": run.get("store_id"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载历史分析失败：%s", exc)
        return {"clarification": "读取刚才的经营分析失败，请重新分析后再发送通知。"}


def clarification_node(state: dict) -> dict:
    return {"final_report": state.get("clarification") or "需要补充更多信息后才能继续。"}


def route_after_notification(state: dict) -> str:
    return "clarify" if state.get("clarification") else "end"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("data_agent", build_data_agent())
    graph.add_node("kb_agent", build_kb_agent())
    graph.add_node("persist_analysis", persist_analysis_node)
    graph.add_node("load_last_analysis", load_last_analysis_node)
    graph.add_node("notification_agent", build_notification_agent())
    graph.add_node("clarification", clarification_node)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor", route_after_supervisor,
        {"data": "data_agent", "kb": "kb_agent", "followup": "load_last_analysis", "clarify": "clarification"},
    )
    graph.add_edge("data_agent", "persist_analysis")
    graph.add_conditional_edges(
        "persist_analysis", route_after_persist_analysis,
        {"notify": "notification_agent", "clarify": "clarification", "end": END},
    )
    graph.add_conditional_edges(
        "load_last_analysis", lambda state: "clarify" if state.get("clarification") else "notify",
        {"clarify": "clarification", "notify": "notification_agent"},
    )
    graph.add_conditional_edges(
        "notification_agent", route_after_notification,
        {"clarify": "clarification", "end": END},
    )
    graph.add_edge("kb_agent", END)
    graph.add_edge("clarification", END)

    compiled = graph.compile()
    logger.info("顶层图构建完成：data/kb/followup + notification 子图")
    return compiled


agent = build_graph()
