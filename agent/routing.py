"""
统一意图路由配置（#6）
====================
收敛 supervisor / report / analysis 三处独立词表为**一份配置 + 一个判定函数**，
消除"双列表命中冲突"与"判定逻辑不同步"（supervisor 与 report 判定不一致曾导致
同一问题走不同报告模板）。

判定规则（默认 RAG，知识词优先）：
1. 命中 KNOWLEDGE_KEYS（强知识词：制度/手册/话术/报销/考勤…）→ kb（知识问答）
   —— 修复冲突用例："报销流程数据"同时命中知识词"报销"与数据词"数据"，按知识问答；
2. 否则命中 DATA_KEYS（指标导向词）→ data（经营分析）；
3. 未命中任一 → kb（默认 RAG，避免答非所问）。

所有消费方必须调用本模块的 `resolve_intent` / `is_data_question` / `is_market_question`，
禁止在业务代码里自行再维护一份关键词表。
"""
from __future__ import annotations

# 知识类问题关键词：命中则跳过经营分析，直接知识检索回答（制度/手册/话术等）
# 注意：这些词优先于数据词（见 resolve_intent 规则 1）
KNOWLEDGE_KEYS = [
    "手册", "制度", "规定", "话术", "报销", "考勤", "处罚", "晋升", "薪资", "薪酬",
    "绩效", "请假", "休假", "流程", "标准", "福利", "培训", "合同", "招聘", "离职",
    "入职", "着装", "工服", "印章", "出差", "值班", "排班", "员工关系",
]

# 数据类问题关键词（指标导向）：命中任一才走「工具查询 + 经营诊断」链路；
# 未命中则默认走 RAG 知识问答（制度/手册/话术/流程等非数据类提问）
DATA_KEYS = [
    # 核心经营指标
    "营业额", "gmv", "销售额", "销售量", "销量", "营收", "收入", "订单", "单量", "客单价", "单数",
    "环比", "同比", "增长", "下降", "上升", "下滑", "趋势", "涨了", "跌了",
    # 推广投放
    "推广", "广告", "投放", "roi", "转化", "点击", "花费", "消耗", "预算",
    # 客流/市场
    "客流", "曝光", "访问", "意向", "引流",
    # 交易/咨询
    "交易", "核销", "退款", "咨询", "留咨", "回复率",
    # 排名/对比
    "排名", "排行", "表现", "对比", "最好", "最差", "top",
    # 数据操作
    "数据", "报表", "明细", "刷新", "更新", "下载", "指标", "金额",
    "利润", "毛利", "净利", "成本", "费用", "储值", "团建套餐",
]

# 门店排名/市场对比类问题关键词（决定报告输入是市场数据而非单店 sales）
MARKET_KEYS = [
    "排名", "排行", "综合", "哪家店", "门店对比", "客流", "交易", "咨询",
    "表现", "门店排行", "top", "哪一家", "哪个门店", "最多", "最少", "最高", "最低",
]

SALES_RANKING_METRICS = ("销量", "销售量", "订单", "单量", "营业额", "销售额", "gmv", "营收")
RANKING_CUES = ("排名", "排行", "哪家", "哪一家", "哪个门店", "最多", "最少", "最高", "最低", "top")
DATA_ADVICE_KEYS = ("原因", "为什么", "建议", "策略", "优化", "提升", "诊断", "怎么办", "如何")


def resolve_intent(question: str) -> str:
    """统一意图判定：返回 "data"（经营分析）或 "kb"（知识问答）。

    知识词优先：同时命中知识词与数据词的冲突问题（如"报销流程数据"）判知识问答。
    """
    q = (question or "").lower()
    if any(k in q for k in KNOWLEDGE_KEYS):
        return "kb"
    if any(k in q for k in DATA_KEYS):
        return "data"
    return "kb"


def is_data_question(question: str) -> bool:
    """兼容旧 API：是否数据类问题（== resolve_intent(question) == "data"）。"""
    return resolve_intent(question) == "data"


def is_market_question(question: str) -> bool:
    """是否门店排名/市场对比类问题（决定报告取市场数据维度）。"""
    q = (question or "").lower()
    return any(k in q for k in MARKET_KEYS)


def is_sales_ranking_question(question: str) -> bool:
    """是否需要按门店查询销售/销量排名（而非综合经营排名）。"""
    q = (question or "").lower()
    return any(k in q for k in SALES_RANKING_METRICS) and any(k in q for k in RANKING_CUES)


def should_retrieve_operation_knowledge(question: str, intent_type: str) -> bool:
    """知识库只服务制度问答或数据诊断建议，纯数据事实查询不检索。"""
    if intent_type == "kb":
        return True
    q = (question or "").lower()
    return any(k in q for k in DATA_ADVICE_KEYS)
