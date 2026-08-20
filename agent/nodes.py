"""
Agent 节点实现
=============
流程：intent(意图+工具决策) → [tools 回环] → analysis(确定性分析) → rag(知识检索) → report(报告)

- intent_node  : LLM bind_tools 决定是否/调用哪些工具
- tools_node   : 自定义工具执行节点——完整结果写入 state.query_result，
                  messages 中只保留截断版 ToolMessage（控制 token 消耗）
- analysis_node: 从 state.query_result 取数，确定性调用 analysis_business_data（Pandas）
- rag_node     : 以问题+分析结论检索运营知识库
- report_node  : LLM 综合 数据+分析+知识 生成 markdown 诊断报告
- route_after_intent: 条件边，判断最后一条消息是否有 tool_calls
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.routing import (
    is_data_question,
    is_market_question,
    is_sales_ranking_question,
    resolve_intent,
    should_retrieve_operation_knowledge,
)
from config.llm_factory import create_llm
from config.settings import settings
from tools import ALL_TOOLS
from tools.analysis_tool import analysis_business_data
from tools.browser_tool import update_campaign_budget
from tools.database_tool import get_campaign_data, get_sales_data, get_store_sales_ranking
from tools.market_data_tool import get_consult_data, get_store_ranking, get_traffic_data, get_transaction_data
from tools.rag_tool import search_operation_knowledge

logger = logging.getLogger(__name__)

# ToolMessage 截断长度：完整工具结果在 state.query_result，消息里只留摘要防 token 爆炸
TOOL_MESSAGE_MAX_CHARS = 1500
DATA_REPORT_MAX_TOKENS = 1200
RAG_REWRITE_TRIGGER_SCORE = 0.70

# 工具名 → query_result 键（结构化存放供分析节点确定性读取）
_QUERY_KEYS = {
    "get_sales_data": "sales",
    "get_store_sales_ranking": "sales_ranking",
    "get_campaign_data": "campaign",
}

KNOWLEDGE_REPORT_PROMPT = """你是连锁门店的内部知识助手，服务对象是门店员工（新员工、店员、店长等）。请用**口语化、自然、清晰**的方式回答制度/流程/话术类问题，让提问者一眼就能看懂、直接用。

回答要求：
1. **结论优先**：先直接给出答案（一句话说清核心），再展开要点；用短段落和「-」要点，适合手机快速阅读；
2. **整合编排**：把知识库检索到的多条内容融合成连贯的回答，用自己的话转述，**禁止大段照抄原文**，禁止出现"知识库中写了/未检索到/文档提到"这类元描述；**若输入中含"最近对话历史"，优先引用其中已确认的信息**（如上一轮已回答过的数字/结论），本轮提问是追问时直接用上文作答，不必重复展开；
3. **关键信息精确**：涉及时间、金额、天数、分数等，按知识库原文精确给出并加粗，例如「**9:00-18:00**」「**连续 3 次**」；
4. **内容不足/不相关的处理**：检索到的内容与问题**不相关**（如问公司介绍却只检索到制度条款）时，**如实说明"知识库暂无该类资料"**，并给出合理操作指引（如"可咨询店长/综合管理中心获取"），注明"以公司最新资料为准"；**严禁用无关内容硬凑回答**。知识库有明确条款但内容不完整时，结合常识补充解释并注明"以门店最新制度/人事确认为准"。
5. **来源简短**：结尾一行注明依据来源（如「依据：《门店晋升制度》（2026-07-20）」，不加章节罗列）；
6. 全文（含标题）不超过 350 字，整体像一位熟悉店规的老员工在回答新员工。"""

INTENT_SYSTEM_PROMPT = """你是连锁门店经营分析助手。根据用户问题决定需要查询哪些数据。

可用工具：
- get_sales_data：查询门店销售数据（营业额/订单数/客单价/环比）
- get_store_sales_ranking：查询跨门店销量/订单量/营业额排名
- get_campaign_data：查询推广数据（消耗/点击/转化/ROI）
- get_traffic_data：查询客流数据（曝光/访问/意向转化，门店维度，可排名）
- get_transaction_data：查询交易数据（下单/核销/退款金额，门店维度，可排名）
- get_consult_data：查询在线咨询数据（咨询人数/留咨/回复率，门店维度，可排名）
- get_store_ranking：门店综合排名（客流+交易+咨询加权打分）
- refresh_market_data：刷新美团经营数据并入库（用户要求「更新/刷新/下载数据」时调用）
- analysis_business_data：对数据做指标计算与归因（Pandas 确定性计算）
- search_operation_knowledge：检索运营知识库（SOP/推广策略/活动规则/诊断案例）
- update_campaign_budget：为「调整推广预算」生成执行计划（dry-run，返回 plan_id）。**该工具绝不直接修改任何数据**——真正执行必须由用户在界面点击确认（或调用确认接口）后才会发生。生成计划后，回答中要明确告知用户存在待确认的执行计划，引导用户确认或忽略。

要求：
1. 优先调用数据查询工具获取真实数据；
2. 用户问「门店排名/综合表现/哪家店好/客流/交易/咨询」时，调用 get_traffic_data/get_transaction_data/get_consult_data/get_store_ranking（rank=True）；
3. 从问题中提取 store_id、时间范围（days）等参数，无法确定时使用默认值；
4. 数据齐备后停止调用工具，等待后续分析节点处理；
5. 用户问制度/考勤/手册/流程/话术/报销等知识类问题时，**禁止调用数据工具**，不产出任何工具调用，直接等待知识检索节点回答。"""

REPORT_SYSTEM_PROMPT = """你是经营顾问。基于【数据概览】【分析结果】【知识库建议】生成精炼报告。

严格按下面 5 段顺序输出，每段开头必须是中文方括号【关键词】：

【结论摘要】2~3 条核心结论
【关键指标】3~4 条关键数字（必须是本次问题相关指标）
【原因归因】2~3 条原因，带量化证据
【建议】2~3 条可立即执行的动作
【风险提示】1~2 条风险

硬性要求：
1. 严格按 5 段顺序，每段开头【关键词】方括号包裹，不得加 #/✦/★/「」 等其他符号；
2. 不得吞字（关键词完整写出）；
3. 全文不超过 600 字；
4. 若【数据概览】中存在待确认执行计划（pending_plans，含 plan_id），在【建议】末尾追加一行：
   ⚠️ 已生成执行计划（计划号 …）：campaign X 预算 A→B，请点击「确认执行」或忽略（10 分钟内有效）。"""


# ---------------------------------------------------------------- intent
def intent_node(state: dict) -> dict:
    """意图分析 + 工具调用决策。

    路由策略（默认 RAG，统一判定见 agent/routing.py::resolve_intent）：
    - 数据类问题（resolve_intent=="data"）→ 绑全部工具，由 LLM 决策查询哪些数据
    - 非数据类问题（制度/手册/话术/流程等）→ 不绑工具，直接走 analysis→rag→report 知识问答
    """
    question = state.get("user_question", "") or ""
    if not is_data_question(question):
        logger.info("非数据类问题（默认走 RAG 知识问答）：%s", question[:60])
        return {"messages": [AIMessage(content="知识类问题，跳过数据查询，直接检索知识库回答")]}

    # 确定性计划完成后直接进入分析，不能在 tools → intent 回边重复计划查询。
    if state.get("query_result") and not getattr((state.get("messages") or [None])[-1], "tool_calls", None):
        return {"messages": [AIMessage(content="数据查询完成，进入确定性分析")]}

    # 常规指标查询的工具集合及参数可以由规则稳定确定。原实现即使 LLM 不调用工具，
    # analysis_node 也会再兜底查 sales/campaign，因此这一次 ReAct 决策既慢又贵。
    # 涉及执行计划的请求仍保留 LLM tool calling，避免擅自猜测 campaign_id/预算。
    planned = _build_data_tool_plan(question, state.get("store_id"))
    if planned is not None:
        logger.info("确定性数据查询计划：%s", [p["name"] for p in planned])
        return {"tool_plan": planned}

    llm = create_llm().bind_tools(ALL_TOOLS)
    messages = list(state.get("messages", []) or [])
    # 门店解析结果注入（#6）："XX店营业额"不再让 LLM 猜 store_id=1
    store_id = state.get("store_id")
    if store_id:
        hint = HumanMessage(content=f"（用户问的是 {store_id} 号门店，请优先以 store_id={store_id} 查询相关数据；若数据工具不支持该门店则如实说明）")
        logger.info("intent 注入目标门店：store_id=%s", store_id)
        response = llm.invoke([SystemMessage(content=INTENT_SYSTEM_PROMPT)] + messages[-1:] + [hint])
    else:
        # 数据查询不需要把历史报告再送进工具规划模型；追问所需上下文由最终报告节点处理。
        response = llm.invoke([SystemMessage(content=INTENT_SYSTEM_PROMPT)] + messages[-1:])
    return {"messages": [response]}


# ---------------------------------------------------------------- tools 回环
def route_after_intent(state: dict) -> str:
    """条件边：最后一条消息是否请求工具。"""
    messages = state.get("messages", []) or []
    last = messages[-1] if messages else None
    if state.get("tool_plan") or getattr(last, "tool_calls", None):
        return "tools"
    return "analysis"


def _tool_by_name(name: str):
    for t in ALL_TOOLS:
        if getattr(t, "name", None) == name:
            return t
    return None


def _build_data_tool_plan(question: str, store_id: int | None) -> list[dict] | None:
    """为非执行型数据问题生成确定性查询计划。

    返回 None 表示必须交给 LLM（目前仅预算调整等有副作用的计划生成）。返回空列表
    不使用，保证普通问题至少会查询一种真实业务数据。
    """
    q = (question or "").lower()
    if any(k in q for k in ("调整", "修改", "变更", "执行", "提高预算", "降低预算", "增加预算", "减少预算")):
        return None
    # 明确起止日期交给 LLM 提取 start_date/end_date，避免规则解析造成口径错误。
    if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", q):
        return None

    sid = store_id or 1
    days = _extract_query_days(q)
    market = is_market_question(q)
    campaign_words = ("推广", "广告", "投放", "roi", "点击", "花费", "消耗", "转化")
    sales_words = ("营业额", "gmv", "销售额", "营收", "收入", "订单", "单量", "客单价", "环比", "同比", "增长", "下降", "趋势", "金额")
    plan: list[dict] = []
    if is_sales_ranking_question(q):
        metric = "sales_volume" if any(k in q for k in ("销量", "销售量")) else "gmv"
        plan = [{"name": "get_store_sales_ranking", "args": {"days": days, "metric": metric, "top_n": 10}}]
    elif market:
        plan = [
            {"name": "get_store_ranking", "args": {"top_n": 10}},
            {"name": "get_traffic_data", "args": {"rank": True}},
            {"name": "get_transaction_data", "args": {"rank": True}},
            {"name": "get_consult_data", "args": {"rank": True}},
        ]
    else:
        if any(k in q for k in sales_words):
            plan.append({"name": "get_sales_data", "args": {"store_id": sid, "days": days}})
        if any(k in q for k in campaign_words):
            plan.append({"name": "get_campaign_data", "args": {"store_id": sid, "days": days}})
    return plan or [{"name": "get_sales_data", "args": {"store_id": sid, "days": days}}]


def _extract_query_days(question: str) -> int:
    """提取常见相对时间窗；无法识别时保持历史默认 7 天。"""
    match = re.search(r"(?:最近|近|过去)(\d{1,3})(?:天|日)", question)
    if match:
        return max(1, min(int(match.group(1)), 365))
    if "本月" in question or "这个月" in question:
        return 30
    if "昨天" in question:
        return 1
    return 7


def tools_node(state: dict) -> dict:
    """
    自定义工具执行节点（替代默认 ToolNode）：
    - 完整执行结果写入 state.query_result（结构化，供分析节点确定性读取）
    - messages 中仅保留截断版 ToolMessage（防止 890 个推广等大结果反复重发撑爆 token）
    """
    messages = list(state.get("messages", []) or [])
    last = messages[-1] if messages else None
    planned_calls = list(state.get("tool_plan") or [])
    tool_calls = planned_calls or (getattr(last, "tool_calls", None) or [])
    query_result = dict(state.get("query_result", {}) or {})
    pending_plans = list(state.get("pending_plans", []) or [])
    tool_messages: list[ToolMessage] = []

    for tc in tool_calls:
        tool = _tool_by_name(tc["name"])
        if tool is None:
            result = {"success": False, "data": {}, "error": f"工具不存在: {tc['name']}"}
        else:
            try:
                result = tool.invoke(tc["args"] or {})
            except Exception as exc:  # noqa: BLE001
                logger.error("工具 %s 执行失败: %s", tc["name"], exc)
                # #8：错误信息脱敏（防 SQL/密钥细节暴露给 LLM/前端）
                from tools.sanitize import sanitize_error

                result = {"success": False, "data": {}, "error": sanitize_error(str(exc))}

        # 完整结果入 state（供 analysis_node 使用）
        key = _QUERY_KEYS.get(tc["name"])
        if key:
            query_result[key] = result
        else:
            query_result[tc["name"]] = result

        # 自动化执行工具：捕获生成的执行计划（dry-run，待用户确认）到 state.pending_plans
        if tc["name"] == "update_campaign_budget" and isinstance(result, dict):
            plan = (result.get("data") or {}) if result.get("success") else {}
            if plan.get("plan_id") and all(p.get("plan_id") != plan["plan_id"] for p in pending_plans):
                pending_plans.append(plan)
                logger.info("捕获待确认执行计划：%s（campaign %s 预算 %s → %s）",
                            plan["plan_id"], plan.get("campaign_id"), plan.get("old_budget"), plan.get("new_budget"))

        # 审计：工具调用记录（执行动作可追溯）
        try:
            from config.logging_setup import audit
            audit("tool_call", state.get("user_question", "")[:64],
                  tool=tc["name"], args=tc["args"] or {}, success=result.get("success", True))
        except Exception:  # noqa: BLE001
            pass

        # 截断版 ToolMessage
        content = json.dumps(result, ensure_ascii=False, default=str)
        if len(content) > TOOL_MESSAGE_MAX_CHARS:
            content = content[:TOOL_MESSAGE_MAX_CHARS] + "…(结果已截断，完整数据见分析环节)"
        # 确定性计划不回到 LLM，无需构造 ToolMessage；这也避免把工具结果再注入上下文。
        if not planned_calls:
            tool_messages.append(ToolMessage(content=content, tool_call_id=tc["id"], name=tc["name"]))

    return {"messages": tool_messages, "query_result": query_result, "pending_plans": pending_plans, "tool_plan": []}


# ---------------------------------------------------------------- analysis
def analysis_node(state: dict) -> dict:
    """确定性数据分析：从 state.query_result 取数 → Pandas 计算归因。

    数据来源：
    1. tools_node 写入的 query_result（真实 LLM 工具调用路径）
    2. 兜底：直接调用数据工具（LLM 未调用工具时确定性补查）
    """
    query_result = state.get("query_result", {}) or {}
    sales = query_result.get("sales") or {}
    campaign = query_result.get("campaign") or {}

    # 知识类问题短路：不查销售/推广数据（intent 已判定，这里保持一致兜底）
    if not is_data_question(state.get("user_question", "") or ""):
        logger.info("知识类问题：分析节点跳过数据查询")
        return {"analysis_result": {"data": {}, "factors": [], "metrics": {}}}

    if not sales and not is_market_question(state.get("user_question", "") or ""):
        # 门店解析结果优先（#6），默认 1 号店
        store_id = state.get("store_id") or 1
        logger.info("未获取到工具调用结果，由分析节点直接查询数据（store_id=%s, days=7）", store_id)
        sales = get_sales_data.invoke({"store_id": store_id, "days": 7})
    # 仅当问题确实涉及推广或 LLM 主动取过推广数据时才查，避免销售问答额外聚合一份推广报表。
    if not campaign and any(k in (state.get("user_question", "") or "").lower()
                            for k in ("推广", "广告", "投放", "roi", "点击", "花费", "消耗", "转化")):
        campaign = get_campaign_data.invoke({"store_id": state.get("store_id") or 1})

    analysis = analysis_business_data.invoke(
        {"sales_data": sales, "campaign_data": campaign}
    )

    # 市场数据兜底：销售排名与客流/交易/咨询类问题，LLM 未调用时确定性补查
    question = state.get("user_question", "")
    if is_sales_ranking_question(question):
        if "sales_ranking" not in query_result:
            try:
                query_result["sales_ranking"] = get_store_sales_ranking.invoke({
                    "days": _extract_query_days(question),
                    "metric": "sales_volume" if any(k in question.lower() for k in ("销量", "销售量")) else "gmv",
                    "top_n": 10,
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("兜底查询门店销售排名失败：%s", exc)
    elif is_market_question(question):
        for tool, key in (
            (get_store_ranking, "get_store_ranking"),
            (get_traffic_data, "get_traffic_data"),
            (get_transaction_data, "get_transaction_data"),
            (get_consult_data, "get_consult_data"),
        ):
            if key not in query_result:
                try:
                    kwargs = {"top_n": 10} if key == "get_store_ranking" else {"rank": True}
                    query_result[key] = tool.invoke(kwargs)
                    logger.info("兜底查询 %s ✓", key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("兜底查询 %s 失败：%s", key, exc)

    return {
        "analysis_result": analysis,
        "query_result": {"sales": sales, "campaign": campaign, **{k: v for k, v in query_result.items() if k not in ("sales", "campaign")}},
    }


# ---------------------------------------------------------------- rag
REWRITE_PROMPT = """你是知识库检索查询优化器。用户问题将用于企业内部知识库（制度/公司资料/话术）检索。

任务（一次完成，只输出 JSON）：
1. "queries"：把问题改写为 2 条检索友好的关键词句——陈述式（去掉"多少/怎么/为什么/哪"等疑问词）、
   补充领域关键词（如"门店数量"→"门店数量 门店规模 直营与合作门店"）、保留专有名词（人名/品牌/制度名）。
2. "hypothetical_answer"：写一段 60 字以内的假设答案（允许基于常识推断、允许虚构，仅用于语义向量检索）。

输出格式（严格 JSON）：
{"queries": ["改写1", "改写2"], "hypothetical_answer": "假设答案"}

示例：
问题：公司有多少门店？
输出：{"queries": ["公司的门店数量 门店规模 直营和合作门店分布", "杭州欢愉公司全国直营合作门店总数"], "hypothetical_answer": "杭州欢愉商业经营管理有限公司在全国拥有30多家直营和合作门店，分布在北京、上海、杭州、苏州、常州等城市，总部在杭州。"}"""


def _rewrite_query(question: str) -> tuple[list[str], str]:
    """Query Rewrite + HyDE：LLM 把疑问句改写成检索友好的陈述句，并生成一段假设答案。

    返回 (queries, hyde_answer)；任何失败降级为 (原问题, "")，不影响主流程。
    仅知识问答链路调用（经营分析保持原问题检索，防延迟/稀释）。
    """
    try:
        import json as _json

        llm = create_llm()
        resp = llm.invoke(
            [
                SystemMessage(content=REWRITE_PROMPT),
                HumanMessage(content=f"问题：{question}"),
            ],
            max_tokens=300,
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        # 容错：截取 JSON 块
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return [question], ""
        data = _json.loads(text[start : end + 1])
        queries = [q.strip() for q in data.get("queries", []) if isinstance(q, str) and q.strip()]
        hyde = (data.get("hypothetical_answer") or "").strip()
        return (queries[:3] or [question]), hyde
    except Exception as exc:  # noqa: BLE001
        logger.warning("Query Rewrite 失败，降级用原问题：%s", exc)
        return [question], ""


def _dedup_docs(docs: list[dict], limit: int) -> list[dict]:
    """按内容前缀去重合并（多路检索结果融合）。"""
    seen: set = set()
    out: list[dict] = []
    for d in docs:
        key = str(d.get("content", ""))[:80]
        if key not in seen:
            seen.add(key)
            out.append(d)
        if len(out) >= limit:
            break
    return out


def rag_node(state: dict) -> dict:
    """知识检索：双路检索（知识层用纯问题，避免经营结论稀释知识类查询；
    经验层带分析结论，保证历史报告相关性）。

    低置信知识问题才启用 Query Rewrite + HyDE，并行补充召回；明确命中时只走原问题检索。
    """
    question = state.get("user_question", "")
    analysis = (state.get("analysis_result") or {}).get("data", {}) or {}
    metrics = analysis.get("metrics", {}) or {}
    factors = analysis.get("factors", []) or []

    # 知识层检索：
    # - kb 链路：Query Rewrite + HyDE 多路检索（原问题 BM25 + 改写句/假设答案向量），
    #   解决"公司有多少门店"这类疑问句与文档陈述句的语义鸿沟
    # - data 链路：保持纯原问题（经营结论不被稀释）
    if state.get("intent_type") == "kb":
        # 大多数明确制度名/专有名词可直接命中。先做一次无 LLM 的原问题检索，
        # 低置信度时才支付 Query Rewrite + HyDE 的额外模型调用成本。
        raw_docs = search_operation_knowledge.invoke({"query": question, "top_k": 3})
        best_score = max((float(d.get("score") or 0) for d in raw_docs), default=0.0)
        if len(raw_docs) >= 2 and best_score >= RAG_REWRITE_TRIGGER_SCORE:
            docs = _dedup_docs(raw_docs, 5)
            logger.info("kb 原问题高置信命中：%s 条，最高分 %.3f，跳过改写", len(docs), best_score)
        else:
            rewritten, hyde = _rewrite_query(question)
            cands = [q for q in rewritten if q and q != question]
            if hyde:
                cands.append(hyde)

            async def _parallel_retrieve() -> list[dict]:
                async def _one(q: str) -> list[dict]:
                    try:
                        return await asyncio.to_thread(
                            search_operation_knowledge.invoke, {"query": q, "top_k": 3}
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("检索 %s 失败：%s", q[:30], exc)
                        return []

                pooled = await asyncio.gather(*[_one(q) for q in cands[:3]])
                return [d for batch in pooled for d in batch]

            try:
                pooled = asyncio.run(_parallel_retrieve())
            except Exception as exc:  # noqa: BLE001 极端情况（无事件循环等）退串行
                logger.warning("kb 并行检索失败，退串行：%s", exc)
                pooled = []
                for q in cands[:3]:
                    try:
                        pooled.extend(search_operation_knowledge.invoke({"query": q, "top_k": 3}))
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning("检索 %s 失败：%s", q[:30], exc2)
            docs = _dedup_docs(list(raw_docs) + pooled, 6)
            logger.info("kb 低置信检索：原问题 + 改写%s + HyDE%s → %s 条去重",
                        len(rewritten), "✓" if hyde else "✗", len(docs))
    elif should_retrieve_operation_knowledge(question, state.get("intent_type", "data")):
        docs = search_operation_knowledge.invoke({"query": question, "top_k": 5})
    else:
        docs = []
        logger.info("纯数据事实查询：跳过内部知识库检索：%s", question[:60])

    # 经验层：问题 + 分析结论（召回相关历史诊断报告）
    # 仅经营分析链路（intent=data）需要；知识问答（kb）不查经验层——
    # 否则"介绍公司"会命中经营历史报告，污染答案 + 增加延迟/tokens
    if state.get("intent_type") != "kb":
        try:
            from rag.retriever import EXPERIENCE_MAX_AGE_DAYS, get_vector_client

            exp_parts = [question]
            if metrics.get("gmv_change_pct") is not None:
                exp_parts.append(f"营业额环比{metrics['gmv_change_pct']}%")
            for f in factors[:2]:
                exp_parts.append(f.get("impact", ""))
            exp_topic = "；".join(p for p in exp_parts if p)

            # #3 同步：经验层检索按门店隔离（report 已带 store_id 元数据），
            # 跨门店历史报告不再污染当前门店归因
            exp_meta = {}
            if state.get("store_id"):
                exp_meta["store_id"] = str(state["store_id"])

            exp_docs = get_vector_client().query(
                exp_topic, top_k=3, filter_type="report", max_age_days=EXPERIENCE_MAX_AGE_DAYS,
                filter_meta=exp_meta or None,
            )
            for e in exp_docs:
                meta = dict(e.get("metadata") or {})
                meta["is_experience"] = True
                e["metadata"] = meta
            if exp_docs:
                docs = list(docs) + exp_docs
                logger.info("经验层命中 %s 条历史报告（max_age=%sd, store=%s）",
                            len(exp_docs), EXPERIENCE_MAX_AGE_DAYS, exp_meta.get("store_id", "all"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("经验层检索失败：%s", exc)

    return {"retrieval_docs": docs}


# ---------------------------------------------------------------- report
# #14 结构化报告：data 链路用 with_structured_output 输出五段 JSON（前端直接渲染，
# 告别"prompt 自律 + 前端正则兜底"与内部字段名泄漏）；kb 链路保持口语化 markdown。

from pydantic import BaseModel, Field


class ReportSections(BaseModel):
    """经营诊断报告五段结构化输出（#14）。"""

    summary: list[str] = Field(..., description="结论摘要：2~3 条核心结论")
    metrics: list[str] = Field(..., description="关键指标：3~4 条本次问题相关关键数字，格式『指标名：数值』")
    factors: list[str] = Field(..., description="原因归因：2~3 条原因，带量化证据")
    actions: list[str] = Field(..., description="建议：2~3 条可立即执行的动作")
    risks: list[str] = Field(..., description="风险提示：1~2 条风险")


REPORT_STRUCTURED_PROMPT = """你是经营顾问。基于【数据概览】【分析结果】【知识库建议】输出结构化经营诊断报告。

输出 JSON 五段（字段见 schema）：
- summary（结论摘要）：2~3 条核心结论，每条一句话；
- metrics（关键指标）：3~4 条本次问题相关关键数字，格式「指标名：数值」（如「营业额：128,540 元，环比 -23.4%」），禁止输出 reply30 等内部字段名；
- factors（原因归因）：2~3 条原因，带量化证据；
- actions（建议）：2~3 条可立即执行的动作；
- risks（风险提示）：1~2 条风险。

硬性要求：
1. 内容面向店长可读，禁止原样引用内部数据结构字段名；
2. **五个字段都必须是字符串数组**（如 "summary": ["结论一", "结论二"]），禁止输出 markdown 代码块（```json 等）或任何多余说明文字；
3. 若【数据概览】中存在待确认执行计划（pending_plans，含 plan_id），在 actions 末尾追加一条：
   「⚠️ 已生成执行计划（计划号 …）：campaign X 预算 A→B，请点击『确认执行』或忽略（10 分钟内有效）」；
4. 全文合计不超过 600 字。"""


def _parse_report_sections(text: str) -> dict:
    """容错解析 LLM 输出的五段 JSON（跨模型稳定，不依赖 tool_choice）。

    提取首个 {...} 块 → json.loads → Pydantic 校验规范化；失败抛 ValueError。
    字段容错：模型可能输出字符串而非数组（如 "1. ...\n2. ..."），按行/序号拆分。
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("输出中未找到 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")

    def _split_items(v) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            lines = [ln.strip() for ln in v.split("\n") if ln.strip()]
            if len(lines) > 1:
                return lines
            # 单行：按序号（1. / 1、）或分号拆分
            parts = re.split(r"\d+[.、]\s*|；|;", v)
            return [p.strip() for p in parts if p.strip()] or [v.strip()]
        return []

    sections = ReportSections(
        summary=_split_items(data.get("summary")),
        metrics=_split_items(data.get("metrics")),
        factors=_split_items(data.get("factors")),
        actions=_split_items(data.get("actions")),
        risks=_split_items(data.get("risks")),
    )
    if not any([sections.summary, sections.metrics, sections.factors, sections.actions, sections.risks]):
        raise ValueError("结构化内容为空")
    return sections.model_dump()


def _sections_to_markdown(sections: dict) -> str:
    """结构化五段 → 标准 markdown（供会话持久化 / 经验层入库 / 旧前端兼容渲染）。"""

    def _block(title: str, items: list) -> str:
        if not items:
            return f"【{title}】\n- 无"
        return f"【{title}】\n" + "\n".join(f"- {i}" for i in items)

    return "\n\n".join([
        _block("结论摘要", sections.get("summary", [])),
        _block("关键指标", sections.get("metrics", [])),
        _block("原因归因", sections.get("factors", [])),
        _block("建议", sections.get("actions", [])),
        _block("风险提示", sections.get("risks", [])),
    ])


def _refresh_prefix(state: dict, report: str) -> str:
    """数据刷新结果前缀（确定性：不依赖 LLM 自觉提及刷新动作）。"""
    refresh = (state.get("query_result") or {}).get("refresh_market_data") or {}
    if isinstance(refresh, dict) and refresh.get("success"):
        data = refresh.get("data") or {}
        ok_parts = [k for k, v in data.items() if isinstance(v, dict) and v.get("success")]
        return f"📥 数据已刷新至最新（{len(ok_parts)} 项：{', '.join(ok_parts)}），可基于最新数据继续分析。\n\n{report}"
    return report


def _merge_stream_text(accumulated: str, content: str) -> str:
    """合并标准增量与少数兼容端点返回的累计流式文本。"""
    if not content:
        return accumulated
    if not accumulated:
        return content
    # OpenAI 标准 SSE 返回增量；部分兼容端点会重复发送“从开头到当前”的累计文本。
    # 若仍直接 append，会把整段答案重复渲染。
    if content == accumulated or accumulated.endswith(content):
        return accumulated
    if content.startswith(accumulated):
        return content
    return accumulated + content


def _stream_report(llm, messages: list, max_tokens: int) -> str:
    """流式 markdown 报告生成（kb 链路 + data 结构化失败回退），含空输出重试兜底。"""
    report = ""
    try:
        for chunk in llm.stream(messages, max_tokens=max_tokens):
            content = chunk.content
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if content:
                report = _merge_stream_text(report, str(content))
    except Exception as exc:  # noqa: BLE001
        logger.warning("report 流式调用异常，回退非流式：%s", exc)
        response = llm.invoke(messages, max_tokens=max_tokens)
        report = response.content if isinstance(response.content, str) else str(response.content)

    report = report.strip()
    if not report:
        logger.warning("report 首轮输出为空（推理链占满 max_tokens），重试一次")
        response = llm.invoke(messages)
        report = response.content if isinstance(response.content, str) else str(response.content)
    return report


def _report_structured(state: dict, llm, messages: list) -> dict:
    """data 链路：结构化五段 JSON 报告（#14）。

    三级策略（跨模型稳定）：
    1. with_structured_output（function calling）——部分模型 thinking mode 不支持
       tool_choice（如 deepseek-v4-flash），会 400 → 继续降级；
    2. prompt 输出 JSON + _parse_report_sections 手动解析（不依赖 tool_choice）；
    3. 流式 markdown 回退。

    返回 {"final_report", "report_sections"}。
    """
    sections: dict | None = None
    try:
        structured_llm = llm.with_structured_output(ReportSections)
        result = structured_llm.invoke(messages, max_tokens=DATA_REPORT_MAX_TOKENS)
        sections = result.model_dump() if hasattr(result, "model_dump") else dict(result or {})
        if not sections:
            raise ValueError("with_structured_output 返回空")
    except Exception as exc:  # noqa: BLE001
        logger.warning("结构化方案一（with_structured_output）失败，转方案二（prompt JSON）：%s", str(exc)[:120])
        try:
            response = llm.invoke(messages, max_tokens=DATA_REPORT_MAX_TOKENS)
            text = response.content if isinstance(response.content, str) else str(response.content)
            sections = _parse_report_sections(text)
        except Exception as exc2:  # noqa: BLE001 连 prompt JSON 也失败 → 流式 markdown
            logger.warning("结构化方案二（prompt JSON）失败，回退流式 markdown：%s", str(exc2)[:120])
            report = _stream_report(llm, messages, max_tokens=DATA_REPORT_MAX_TOKENS)
            # 兜底再结构化：模型可能把 JSON 当 markdown 原样输出（第三方流式不稳），
            # 若仍能解析出五段 → 渲染卡片；否则保留 markdown 文本
            try:
                sec = _parse_report_sections(report)
                return {"final_report": _refresh_prefix(state, _sections_to_markdown(sec)), "report_sections": sec}
            except Exception:  # noqa: BLE001 确非 JSON → 原样 markdown
                return {"final_report": _refresh_prefix(state, report)}

    report = _sections_to_markdown(sections).strip()
    if not report:
        logger.warning("结构化输出为空，回退流式 markdown")
        report = _stream_report(llm, messages, max_tokens=DATA_REPORT_MAX_TOKENS)
        try:
            sec = _parse_report_sections(report)
            return {"final_report": _refresh_prefix(state, _sections_to_markdown(sec)), "report_sections": sec}
        except Exception:  # noqa: BLE001 确非 JSON → 原样 markdown
            return {"final_report": _refresh_prefix(state, report)}

    report = _refresh_prefix(state, report)
    _ingest_report_to_kb(report, state)  # 经验层：经营诊断报告自动入库
    logger.info("报告生成（结构化）：%s 条建议 / %s 条风险", len(sections.get("actions", [])), len(sections.get("risks", [])))
    return {"final_report": report, "report_sections": sections}


def report_node(state: dict) -> dict:
    """报告生成：LLM 综合数据/分析/知识生成报告，随后自动入库（经验层）。

    - 知识类问题（制度/手册/话术）：口语化 markdown（流式），不入经营经验层（#14）
    - 经营诊断：**结构化输出**五段 JSON（#14，前端直接渲染）+ 确定性转 markdown
    数据报告最大输出 1200 tokens：正文受 600 字约束，限制异常推理/重试成本。
    """
    question = state.get("user_question", "") or ""
    # 知识问答判定与 supervisor 保持一致（#6 统一路由）：resolve_intent=="kb" → 知识问答模板
    is_knowledge = resolve_intent(question) == "kb"

    llm = create_llm()
    if is_knowledge:
        messages = [
            SystemMessage(content=KNOWLEDGE_REPORT_PROMPT),
            HumanMessage(content=_build_report_input(state)),
        ]
        report = _stream_report(llm, messages, max_tokens=1200)  # 正文限 350 字，1200 足够
        return {"final_report": report}

    messages = [
        SystemMessage(content=REPORT_STRUCTURED_PROMPT),
        HumanMessage(content=_build_report_input(state)),
    ]
    return _report_structured(state, llm, messages)


def _trim_market(result: dict | None, top_n: int = 10) -> dict:
    """压缩市场数据工具结果：保留 total 汇总 + Top N 门店。"""
    if not result or not isinstance(result, dict):
        return {}
    data = result.get("data") or {}
    out = {"period": data.get("period"), "total": data.get("total")}
    stores = data.get("stores", []) or []
    if "rank" in data:  # 排名结果
        out["rank"] = stores[:top_n]
    else:
        out["stores"] = stores[:top_n]
    return out


def _pending_plans_summary(state: dict) -> list[dict]:
    """待确认执行计划摘要（供报告节点提示用户确认）。"""
    out = []
    for p in (state.get("pending_plans") or [])[:5]:
        if not isinstance(p, dict):
            continue
        out.append({
            "plan_id": p.get("plan_id"),
            "action": p.get("action", "update_campaign_budget"),
            "campaign_id": p.get("campaign_id"),
            "old_budget": p.get("old_budget"),
            "new_budget": p.get("new_budget"),
            "reason": (p.get("reason") or "")[:80],
        })
    return out


def _build_report_input(state: dict) -> str:
    """序列化 state 关键结果供报告节点使用（限制体积，防止大列表撑爆上下文）。

    - 排名/客流/交易/咨询类问题：只传市场数据（ranking/traffic/transaction/consult），
      避免单店 sales 数据干扰排名分析
    - 其他问题：传 sales + campaign（Top10）+ 知识
    - retrieval_docs 截断内容
    """
    query_result = state.get("query_result", {}) or {}
    question = state.get("user_question", "")
    is_market_question_flag = is_market_question(question)

    # 知识问答（kb）链路：只传问题 + 检索文档 + 最近对话历史。
    # kb 无数据/无分析结果，不传 query_result/analysis_result（空壳字典也占 tokens）。
    # recent_history：让多轮追问能引用上文已确认信息（如上一轮已回答的门店数），
    # 避免"介绍完公司后追问门店数"时 LLM 看不到上文而答"暂无"。
    if state.get("intent_type") == "kb":
        recent: list[dict] = []
        for m in (state.get("messages") or [])[-6:]:
            mtype = getattr(m, "type", "")
            content = getattr(m, "content", "")
            if mtype in ("human", "ai") and isinstance(content, str) and content.strip():
                recent.append({
                    "role": "用户" if mtype == "human" else "助手",
                    "content": content[:150],
                })
        return json.dumps({
            "user_question": question,
            "recent_history": recent[-4:],
            "retrieval_docs": [
                {
                    "content": d.get("content", "")[:450],  # 保留条款完整性（kb 需要完整条款）
                    "source": d.get("metadata", {}).get("source", ""),
                    "score": d.get("score"),
                }
                for d in (state.get("retrieval_docs") or [])[:5]
            ],
        }, ensure_ascii=False, default=str)

    if is_sales_ranking_question(question):
        # 销量/销售额排名是纯事实查询：只给对应 MySQL 排名结果，避免混入综合排名或知识条款。
        return json.dumps({
            "user_question": question,
            "query_result": {"sales_ranking": query_result.get("sales_ranking", {})},
            "analysis_result": {},
            "retrieval_docs": [],
            "pending_plans": _pending_plans_summary(state),
        }, ensure_ascii=False, default=str)

    if is_market_question_flag:
        # 排名型：只给市场数据，sales/campaign 不传
        summary = {
            "user_question": question,
            "query_result": {
                "traffic": _trim_market(query_result.get("get_traffic_data")),
                "transaction": _trim_market(query_result.get("get_transaction_data")),
                "consult": _trim_market(query_result.get("get_consult_data")),
                "ranking": _trim_market(query_result.get("get_store_ranking"), 10),
            },
            "analysis_result": state.get("analysis_result", {}),
            "retrieval_docs": [
                {"content": d.get("content", "")[:120], "source": d.get("metadata", {}).get("source", ""), "score": d.get("score")}
                for d in (state.get("retrieval_docs") or [])[:3]
            ],
            "pending_plans": _pending_plans_summary(state),
        }
        return json.dumps(summary, ensure_ascii=False, default=str)

    sales = query_result.get("sales", {}) or {}
    sales_data = sales.get("data", {}) if isinstance(sales, dict) else {}
    # 报告只需趋势、KPI 与少量结构性明细；完整日表/商品表留在 state 供确定性分析，
    # 不再重复送入 LLM 上下文。
    sales_trim = {
        "summary": sales_data.get("summary", {}),
        "daily": (sales_data.get("daily", []) or [])[-7:],
        "top_products": (sales_data.get("top_products", []) or [])[:3],
        "category_breakdown": (sales_data.get("category_breakdown", []) or [])[:5],
    }
    campaign = query_result.get("campaign", {}) or {}
    campaign_data = campaign.get("data", {}) if isinstance(campaign, dict) else {}
    campaigns = campaign_data.get("campaigns", []) or []
    # 保留汇总字段 + Top10（压缩成本）
    campaign_trim = dict(campaign_data)
    campaign_trim["campaigns"] = campaigns[:3]
    campaign_trim["campaign_total_count"] = len(campaigns)

    # 数据刷新结果摘要（供报告说明"已刷新到最新"）
    refresh = query_result.get("refresh_market_data", {}) or {}
    refresh_summary = {}
    if isinstance(refresh, dict) and refresh.get("success") is not None:
        refresh_summary = {
            "success": refresh.get("success"),
            "detail": {k: (v.get("success") if isinstance(v, dict) else v) for k, v in (refresh.get("data") or {}).items()},
        }

    summary = {
        "user_question": question,
        "query_result": {
            "sales": sales_trim,
            "campaign": campaign_trim,
            "traffic": _trim_market(query_result.get("get_traffic_data")),
            "transaction": _trim_market(query_result.get("get_transaction_data")),
            "consult": _trim_market(query_result.get("get_consult_data")),
            "ranking": _trim_market(query_result.get("get_store_ranking"), 10),
            "refresh": refresh_summary,
        },
        "analysis_result": state.get("analysis_result", {}),
        "retrieval_docs": [
            {"content": d.get("content", "")[:120], "source": d.get("metadata", {}).get("source", ""), "score": d.get("score")}
            for d in (state.get("retrieval_docs") or [])[:3]
        ],
        "pending_plans": _pending_plans_summary(state),
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


def _ingest_report_to_kb(report: str, state: dict) -> None:
    """
    历史报告自动入库（经验层，doc_type=report，带时间元数据）。

    时间有效性设计（配合检索端 max_age_days 过滤 + 时间衰减）：
    - metadata.report_date：报告日期（检索时超期剔除/衰减）
    - metadata.report_id：唯一标识（日期+门店+问题指纹）——**同日不同门店/不同问题互不覆盖**（#3）
    - metadata.period / store_id / issue_types：供按门店/异常类型过滤
    - 幂等：入库前仅删除"同店同日同问题"的旧报告（report_id 相同），避免重复提问反复累积
    """
    if not report:
        return
    try:
        from datetime import date

        from langchain_core.documents import Document

        from rag.loader import split_documents
        from rag.retriever import get_vector_client

        analysis = (state.get("analysis_result") or {}).get("data", {}) or {}
        metrics = analysis.get("metrics", {}) or {}
        factors = analysis.get("factors", []) or []
        issue_types = [f.get("type") for f in factors if f.get("type") not in (None, "normal")]
        sales = (state.get("query_result") or {}).get("sales", {}) or {}
        summary = (sales.get("data") or {}).get("summary", {}) if isinstance(sales, dict) else {}
        today = date.today().isoformat()
        # 门店维度：显式解析优先，兜底 sales 汇总里的 store_id
        store_id = str(state.get("store_id") or summary.get("store_id") or "all")
        # 问题指纹：同一问题（含门店）重问 → 覆盖旧报告；不同问题 → 各自保留
        question = state.get("user_question", "") or ""
        issue_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()[:8]
        report_id = f"{date.today().strftime('%Y%m%d')}-s{store_id}-{issue_hash}"

        doc = Document(
            page_content=report,
            metadata={
                "doc_type": "report",
                "source": "auto-generated",
                "report_id": report_id,          # #3 唯一标识（检索/去重/审计）
                "report_date": int(date.today().strftime("%Y%m%d")),  # 整数日期（chroma $gte 仅支持数值）
                "report_date_str": today,
                "period": summary.get("period", ""),
                "store_id": store_id,
                "issue_types": ",".join(issue_types) or "general",
            },
        )
        chunks = split_documents([doc])
        client = get_vector_client()
        # 幂等（#3）：只删"同店同日同问题"的旧报告，不再按 report_date 全清——
        # 修复同日多门店/多问题报告互相覆盖（只留最后一份）的 bug
        # 注意 report_date 在向量库中为整数 YYYYMMDD（chroma $gte 仅支持数值比较）
        client.delete("report", report_date=int(date.today().strftime("%Y%m%d")),
                      store_id=store_id, report_id=report_id)
        client.add_documents(chunks, "report")
        logger.info("历史报告已入库（经验层）：%s chunks（%s, %s）", len(chunks), report_id, today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("历史报告入库失败：%s", exc)
