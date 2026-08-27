"""
Agent State
===========
节点间唯一通信渠道：节点返回的 dict 键合并进 State；messages 使用 add_messages reducer 累积。

- messages       对话历史 + 工具调用消息（AIMessage/ToolMessage 自动累积）
- user_question  原始用户问题
- query_result   数据查询结果（get_sales_data / get_campaign_data 输出）
- analysis_result 分析结果（Pandas 归因）
- retrieval_docs 知识检索结果
- final_report   最终经营诊断报告
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    user_question: str
    intent_type: str          # supervisor 路由标记：data=经营分析 / kb=知识问答
    intent_decision: dict     # 复合意图：primary_intent + requested_actions + 澄清原因
    requested_actions: list[str]
    store_id: int | None      # #6 门店解析结果（"XX店" → store_id，供 intent/analysis/经验层检索）
    query_result: dict
    tool_plan: list[dict]    # 确定性数据查询计划；普通数据问答不再消耗一次 LLM 工具决策
    analysis_result: dict
    retrieval_docs: list
    pending_plans: list       # 旧字段兼容，始终为空
    analysis_run_id: str
    analysis_run: dict        # 持久化后的结构化分析快照
    notification_contact: dict
    notification_knowledge: list
    notification_text: str
    notification_plan_id: str
    pending_notifications: list[dict]
    clarification: str
    session_id: str
    final_report: str
    report_sections: dict     # #14 结构化五段报告（summary/metrics/factors/actions/risks；kb 链路无）
