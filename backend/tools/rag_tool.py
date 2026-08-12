"""
RAG 检索工具：search_operation_knowledge
=======================================
- 查询运营知识库（门店SOP/推广策略/活动规则/历史诊断案例）
- 向量库就绪时走 Chroma/Milvus 检索；未就绪（或未入库）时降级返回内置示例知识
- 供 LLM ReAct 循环自主调用，也由 rag 节点确定性调用
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from config.settings import settings
from rag.retriever import get_vector_client

logger = logging.getLogger(__name__)

# 内置示例知识（向量库未入库/不可用时的降级内容，标注 fallback）
_FALLBACK_KNOWLEDGE = [
    {
        "content": "预算消耗超过 80% 时触发预警：评估是否追加预算或调低出价。消耗过快（3 天花完 80%）优先排查出价是否过高、素材是否爆量；消耗过慢（7 天不足 30%）优先排查定向过窄、出价过低、素材点击率低。ROI<1.5 应降低预算 30% 并排查落地页与商品竞争力。",
        "metadata": {"doc_type": "strategy", "source": "推广优化策略.md"},
        "score": 0.95,
        "fallback": True,
    },
    {
        "content": "客单价下滑优先检查商品结构（是否低毛利商品占比升高或核心品类缺货）。品类贡献度分析（帕累托）能快速定位问题品类：某品类销售额下滑占整体下滑 60% 以上时，应优先处理该品类（供应链/上架/活动）。",
        "metadata": {"doc_type": "case", "source": "ROI异常分析案例.md"},
        "score": 0.93,
        "fallback": True,
    },
    {
        "content": "单日营业额环比下降超过 15%，需在 24 小时内提交异常分析报告。归因检查清单：先看大盘（营业额/订单数/客单价/转化率），再看结构（品类贡献、时段、渠道），后看活动（预算消耗曲线、活动生命周期），最后看外部（竞对、天气、节假日）。",
        "metadata": {"doc_type": "sop", "source": "门店运营SOP.md"},
        "score": 0.91,
        "fallback": True,
    },
]


@tool
def search_operation_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索运营知识库（门店SOP/推广策略/活动规则/历史诊断案例），返回知识片段列表。

    参数：query 检索主题（如"推广预算花超如何调整"）；top_k 返回条数。
    返回：[{content, metadata:{doc_type, source}, score}]。
    """
    results: list[dict] = []
    if settings.mock_mode:
        # Mock 模式：不触达向量库（避免下载嵌入模型），直接返回内置示例知识
        return _FALLBACK_KNOWLEDGE

    try:
        client = get_vector_client()
        results = client.query(query, top_k)
    except Exception as exc:  # 向量库未就绪（依赖未装/未建库）
        logger.warning("向量库检索不可用（%s），使用内置示例知识", exc)

    if not results:
        logger.info("知识库未命中（或未入库），降级返回内置示例知识")
        return _FALLBACK_KNOWLEDGE

    return results
