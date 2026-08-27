"""店长通知子图：只生成、校验和持久化草稿，不拥有真实发送权限。"""
from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from sqlalchemy import select

from agent.state import AgentState
from config.llm_factory import create_llm
from database.models import Store
from database.mysql import get_session_factory
from services.contacts import get_active_contact
from services.notifications import (
    create_notification_plan,
    validate_notification_facts,
)
from tools.rag_tool import search_operation_knowledge

logger = logging.getLogger(__name__)


class NotificationSections(BaseModel):
    title: str = Field(description="不超过 24 字的纯文本标题")
    situation: str = Field(description="客观描述经营情况，不编造数字")
    judgement: str = Field(description="结合数据和知识给出判断")
    actions: list[str] = Field(description="2-3 条可执行建议")
    closing: str = Field(description="礼貌结尾，不强加截止时间")


def _analysis_payload(state: dict) -> dict:
    return state.get("analysis_run") or {}


def resolve_recipient_node(state: dict) -> dict:
    run = _analysis_payload(state)
    store_id = state.get("store_id") or run.get("store_id")
    if not store_id:
        return {"clarification": "未能确定目标门店，请先说明需要通知哪家门店的店长。"}
    with get_session_factory()() as session:
        contact = get_active_contact(int(store_id), session)
        store = session.execute(select(Store).where(Store.id == int(store_id))).scalars().first()
        if not contact:
            name = store.store_name if store else f"{store_id} 号门店"
            return {"clarification": f"{name} 尚未导入店长联系人，请先在联系人管理中上传对照表。"}
        return {
            "store_id": int(store_id),
            "notification_contact": {
                "manager_name": contact.manager_name,
                "intended_recipient": f"{contact.manager_name}（{store.store_name if store else contact.store_name}店长）",
                "store_name": store.store_name if store else contact.store_name,
            },
        }


def route_after_recipient(state: dict) -> str:
    return "clarify" if state.get("clarification") else "retrieve"


def retrieve_action_knowledge_node(state: dict) -> dict:
    run = _analysis_payload(state)
    payload = run.get("payload") or run
    factors = payload.get("factors") or []
    report_sections = payload.get("report_sections") or {}
    topic = "；".join(
        [str(f.get("impact") or f.get("type") or "") for f in factors[:2]]
        + [str(x) for x in (report_sections.get("actions") or [])[:2]]
    )
    if not topic:
        return {"notification_knowledge": []}
    try:
        docs = search_operation_knowledge.invoke({"query": topic, "top_k": 3})
    except Exception as exc:  # noqa: BLE001
        logger.warning("通知话术知识检索失败：%s", exc)
        docs = []
    return {"notification_knowledge": docs}


def _fallback_sections(payload: dict) -> NotificationSections:
    report_sections = payload.get("report_sections") or {}
    summary = report_sections.get("summary") or []
    factors = report_sections.get("factors") or []
    actions = report_sections.get("actions") or []
    return NotificationSections(
        title="经营情况沟通",
        situation="；".join(summary[:2]) or "本次经营数据已完成分析。",
        judgement="；".join(factors[:2]) or "请结合门店现场情况核实并跟进。",
        actions=[str(a) for a in actions[:3]] or ["结合本次数据核查门店现场执行情况。"],
        closing="辛苦结合实际情况推进，并在后续沟通中同步进展。",
    )


def _render_text(contact: dict, payload: dict, sections: NotificationSections) -> str:
    period = payload.get("period") or "本次统计周期"
    metrics = (payload.get("report_sections") or {}).get("metrics") or []
    metric_text = "\n".join(f"- {item}" for item in metrics[:4]) or "- 详见本次经营分析结果"
    actions = "\n".join(f"{i}. {item}" for i, item in enumerate(sections.actions[:3], start=1))
    return (
        f"【{sections.title.strip()[:24] or '经营情况沟通'}】\n"
        f"店长：{contact['manager_name']}\n"
        f"门店：{contact['store_name']}\n"
        f"统计周期：{period}\n\n"
        f"一、关键情况\n{metric_text}\n\n"
        f"二、情况说明\n{sections.situation.strip()}\n\n"
        f"三、分析判断\n{sections.judgement.strip()}\n\n"
        f"四、建议动作\n{actions}\n\n"
        f"{sections.closing.strip()}"
    )


def compose_text_node(state: dict) -> dict:
    run = _analysis_payload(state)
    payload = run.get("payload") or run
    contact = state.get("notification_contact") or {}
    knowledge = [
        {"content": (d.get("content") or "")[:220], "source": (d.get("metadata") or {}).get("source", "")}
        for d in (state.get("notification_knowledge") or [])[:3] if isinstance(d, dict)
    ]
    fallback = _fallback_sections(payload)
    try:
        prompt = (
            "你是连锁门店运营沟通助手。仅根据输入的确定性指标、报告结论和运营知识，"
            "为店长生成简洁、专业、可执行的文字通知。不得编造数字、不得写手机号、"
            "不得强制添加截止时间。输出必须匹配 schema。\n"
            + json.dumps({"payload": payload, "knowledge": knowledge}, ensure_ascii=False, default=str)
        )
        llm = create_llm().with_structured_output(NotificationSections)
        result = llm.invoke([SystemMessage(content=prompt), HumanMessage(content="请生成通知话术")])
        sections = result if isinstance(result, NotificationSections) else NotificationSections.model_validate(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("通知话术结构化生成失败，使用数据驱动模板：%s", str(exc)[:120])
        sections = fallback
    text = _render_text(contact, payload, sections)
    try:
        text = validate_notification_facts(text, payload, state.get("store_id"))
    except ValueError:
        # 数字不可追溯时宁可退回确定性报告段落，也不把模型数字带入草稿。
        text = validate_notification_facts(
            _render_text(contact, payload, fallback), payload, state.get("store_id")
        )
    return {"notification_text": text}


def validate_text_node(state: dict) -> dict:
    try:
        payload = (_analysis_payload(state).get("payload") or _analysis_payload(state))
        return {
            "notification_text": validate_notification_facts(
                state.get("notification_text", ""), payload, state.get("store_id")
            )
        }
    except ValueError as exc:
        return {"clarification": f"通知草稿校验失败：{exc}"}


def route_after_validation(state: dict) -> str:
    return "clarify" if state.get("clarification") else "persist"


def persist_draft_node(state: dict) -> dict:
    run = _analysis_payload(state)
    analysis_run_id = state.get("analysis_run_id") or run.get("analysis_run_id")
    if not analysis_run_id:
        return {"clarification": "没有可追溯的经营分析快照，无法生成通知草稿。"}
    contact = state.get("notification_contact") or {}
    with get_session_factory()() as session:
        plan = create_notification_plan(
            analysis_run_id=analysis_run_id,
            session_id=state.get("session_id"),
            store_id=int(state["store_id"]),
            intended_recipient=contact["intended_recipient"],
            message_text=state["notification_text"],
            db_session=session,
        )
    return {"pending_notifications": [plan], "notification_plan_id": plan["plan_id"]}


def build_notification_agent():
    graph = StateGraph(AgentState)
    graph.add_node("resolve_recipient", resolve_recipient_node)
    graph.add_node("retrieve_action_knowledge", retrieve_action_knowledge_node)
    graph.add_node("compose_notification", compose_text_node)
    graph.add_node("validate_notification", validate_text_node)
    graph.add_node("persist_notification", persist_draft_node)

    graph.add_edge(START, "resolve_recipient")
    graph.add_conditional_edges(
        "resolve_recipient", route_after_recipient,
        {"retrieve": "retrieve_action_knowledge", "clarify": END},
    )
    graph.add_edge("retrieve_action_knowledge", "compose_notification")
    graph.add_edge("compose_notification", "validate_notification")
    graph.add_conditional_edges(
        "validate_notification", route_after_validation,
        {"persist": "persist_notification", "clarify": END},
    )
    graph.add_edge("persist_notification", END)
    return graph.compile()
