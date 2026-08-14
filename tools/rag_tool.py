"""
RAG 检索工具：search_operation_knowledge
========================================
- 查询运营知识库（门店SOP/推广策略/活动规则/历史诊断案例）
- 仅真实链路：向量库未就绪/未命中时返回空列表（不注入任何内置示例知识）
- 供 LLM ReAct 循环自主调用，也由 rag 节点确定性调用
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from rag.retriever import get_vector_client

logger = logging.getLogger(__name__)


@tool
def search_operation_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索运营知识库（门店SOP/推广策略/活动规则/历史诊断案例），返回知识片段列表。

    参数：query 检索主题（如"推广预算花超如何调整"）；top_k 返回条数。
    返回：[{content, metadata:{doc_type, source}, score}]；未命中返回空列表。
    """
    try:
        client = get_vector_client()
        # 知识层检索排除 report（历史经营报告）——经验层由 rag_node 单独查询，避免污染知识问答
        results = client.query(query, top_k, exclude_types=["report"])
    except Exception as exc:  # noqa: BLE001 向量库未就绪（依赖未装/未建库）
        logger.warning("向量库检索不可用（%s），返回空结果", exc)
        return []

    if not results:
        logger.info("知识库未命中：%s", query[:60])
    return results
