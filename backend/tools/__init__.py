"""
工具层：统一返回 {"success": bool, "data": ..., "error": ...}
============================================================
- 所有工具通过 @tool 注册为 LangChain 工具，供 LLM bind_tools 调用
- 数据工具在 settings.mock_mode 时返回确定性模拟数据（骨架/无库环境演示）
- 真实模式查询 MySQL（database.mysql）
"""
from __future__ import annotations

from langchain_core.tools import tool

from tools import database_tool, analysis_tool, rag_tool, browser_tool, market_data_tool

# 聚合注册：意图节点 bind_tools 与 ToolNode 共用
# 注意：refresh_market_data（数据采集）不在此列——数据下载改为前端按钮驱动
# （/api/workflow/refresh），避免对话触发的高成本与不可控性
ALL_TOOLS: list = [
    database_tool.get_sales_data,
    database_tool.get_campaign_data,
    market_data_tool.get_traffic_data,
    market_data_tool.get_transaction_data,
    market_data_tool.get_consult_data,
    market_data_tool.get_store_ranking,
    analysis_tool.analysis_business_data,
    rag_tool.search_operation_knowledge,
    browser_tool.update_campaign_budget,
]

__all__ = ["ALL_TOOLS"]
