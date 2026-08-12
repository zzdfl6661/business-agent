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
    query_result: dict
    analysis_result: dict
    retrieval_docs: list
    final_report: str
