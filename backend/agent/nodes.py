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

import json
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from config.llm_factory import create_llm
from config.settings import settings
from tools import ALL_TOOLS
from tools.analysis_tool import analysis_business_data
from tools.browser_tool import update_campaign_budget
from tools.database_tool import get_campaign_data, get_sales_data
from tools.market_data_tool import get_consult_data, get_store_ranking, get_traffic_data, get_transaction_data
from tools.rag_tool import search_operation_knowledge

logger = logging.getLogger(__name__)

# ToolMessage 截断长度：完整工具结果在 state.query_result，消息里只留摘要防 token 爆炸
TOOL_MESSAGE_MAX_CHARS = 1500

# 工具名 → query_result 键（结构化存放供分析节点确定性读取）
_QUERY_KEYS = {"get_sales_data": "sales", "get_campaign_data": "campaign"}

# 知识类问题关键词：命中则跳过经营分析，直接知识检索回答（制度/手册/话术等）
KNOWLEDGE_KEYS = [
    "手册", "制度", "规定", "话术", "报销", "考勤", "处罚", "晋升", "薪资", "薪酬",
    "绩效", "请假", "休假", "流程", "标准", "福利", "培训", "合同", "招聘", "离职",
    "入职", "着装", "工服", "印章", "出差", "值班", "排班", "员工关系",
]

# 数据类问题关键词（指标导向）：命中任一才走「工具查询 + 经营诊断」链路；
# 未命中则默认走 RAG 知识问答（制度/手册/话术/流程等非数据类提问）
DATA_KEYS = [
    # 核心经营指标
    "营业额", "gmv", "销售额", "营收", "收入", "订单", "单量", "客单价", "单数",
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


def is_data_question(question: str) -> bool:
    """数据类问题判定（确定性）：命中 DATA_KEYS 才走经营分析链路；否则默认 RAG 知识问答。"""
    q = (question or "").lower()
    return any(k in q for k in DATA_KEYS)

KNOWLEDGE_REPORT_PROMPT = """你是连锁门店的内部知识助手，服务对象是门店员工（新员工、店员、店长等）。请用**口语化、自然、清晰**的方式回答制度/流程/话术类问题，让提问者一眼就能看懂、直接用。

回答要求：
1. **结论优先**：先直接给出答案（一句话说清核心），再展开要点；用短段落和「-」要点，适合手机快速阅读；
2. **整合编排**：把知识库检索到的多条内容融合成连贯的回答，用自己的话转述，**禁止大段照抄原文**，禁止出现"知识库中写了/未检索到/文档提到"这类元描述；
3. **关键信息精确**：涉及时间、金额、天数、分数等，按知识库原文精确给出并加粗，例如「**9:00-18:00**」「**连续 3 次**」；
4. **内容不足时的处理**：知识库没有明确条款时，结合常识给出合理解释和操作指引（如"先找店长确认""人事补卡流程"），并注明"以门店最新制度/人事确认为准"，不要反复强调"知识库没有"；
5. **来源简短**：结尾一行注明依据来源（如「依据：《门店晋升制度》（2026-07-20）」，不加章节罗列）；
6. 全文（含标题）不超过 350 字，整体像一位熟悉店规的老员工在回答新员工。"""

INTENT_SYSTEM_PROMPT = """你是连锁门店经营分析助手。根据用户问题决定需要查询哪些数据。

可用工具：
- get_sales_data：查询门店销售数据（营业额/订单数/客单价/环比）
- get_campaign_data：查询推广数据（消耗/点击/转化/ROI）
- get_traffic_data：查询客流数据（曝光/访问/意向转化，门店维度，可排名）
- get_transaction_data：查询交易数据（下单/核销/退款金额，门店维度，可排名）
- get_consult_data：查询在线咨询数据（咨询人数/留咨/回复率，门店维度，可排名）
- get_store_ranking：门店综合排名（客流+交易+咨询加权打分）
- refresh_market_data：刷新美团经营数据并入库（用户要求「更新/刷新/下载数据」时调用）
- analysis_business_data：对数据做指标计算与归因（Pandas 确定性计算）
- search_operation_knowledge：检索运营知识库（SOP/推广策略/活动规则/诊断案例）
- update_campaign_budget：调整推广预算（自动化执行，必须用户确认才执行）

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
3. 全文不超过 600 字。"""


# ---------------------------------------------------------------- intent
def intent_node(state: dict) -> dict:
    """意图分析 + 工具调用决策。

    路由策略（默认 RAG）：
    - 数据类问题（命中 DATA_KEYS）→ 绑全部工具，由 LLM 决策查询哪些数据
    - 非数据类问题（制度/手册/话术/流程等）→ 不绑工具，直接走 analysis→rag→report 知识问答
    """
    question = state.get("user_question", "") or ""
    if not is_data_question(question):
        logger.info("非数据类问题（默认走 RAG 知识问答）：%s", question[:60])
        return {"messages": [AIMessage(content="知识类问题，跳过数据查询，直接检索知识库回答")]}

    llm = create_llm().bind_tools(ALL_TOOLS)
    messages = list(state.get("messages", []) or [])
    response = llm.invoke([SystemMessage(content=INTENT_SYSTEM_PROMPT)] + messages)
    return {"messages": [response]}


# ---------------------------------------------------------------- tools 回环
def route_after_intent(state: dict) -> str:
    """条件边：最后一条消息是否请求工具。"""
    messages = state.get("messages", []) or []
    last = messages[-1] if messages else None
    if getattr(last, "tool_calls", None):
        return "tools"
    return "analysis"


def _tool_by_name(name: str):
    for t in ALL_TOOLS:
        if getattr(t, "name", None) == name:
            return t
    return None


def tools_node(state: dict) -> dict:
    """
    自定义工具执行节点（替代默认 ToolNode）：
    - 完整执行结果写入 state.query_result（结构化，供分析节点确定性读取）
    - messages 中仅保留截断版 ToolMessage（防止 890 个推广等大结果反复重发撑爆 token）
    """
    messages = list(state.get("messages", []) or [])
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    query_result = dict(state.get("query_result", {}) or {})
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
                result = {"success": False, "data": {}, "error": str(exc)}

        # 完整结果入 state（供 analysis_node 使用）
        key = _QUERY_KEYS.get(tc["name"])
        if key:
            query_result[key] = result
        else:
            query_result[tc["name"]] = result

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
        tool_messages.append(ToolMessage(content=content, tool_call_id=tc["id"], name=tc["name"]))

    return {"messages": tool_messages, "query_result": query_result}


# ---------------------------------------------------------------- analysis
def analysis_node(state: dict) -> dict:
    """确定性数据分析：从 state.query_result 取数 → Pandas 计算归因。

    数据来源：
    1. tools_node 写入的 query_result（真实 LLM 工具调用路径）
    2. 兜底：直接调用数据工具（Mock LLM 不产生工具调用时，real/mock 数据均适用）
    """
    query_result = state.get("query_result", {}) or {}
    sales = query_result.get("sales") or {}
    campaign = query_result.get("campaign") or {}

    # 知识类问题短路：不查销售/推广数据（intent 已判定，这里保持一致兜底）
    if not is_data_question(state.get("user_question", "") or ""):
        logger.info("知识类问题：分析节点跳过数据查询")
        return {"analysis_result": {"data": {}, "factors": [], "metrics": {}}}

    if not sales:
        logger.info("未获取到工具调用结果，由分析节点直接查询数据（store_id=1, days=7）")
        sales = get_sales_data.invoke({"store_id": 1, "days": 7})
    if not campaign:
        campaign = get_campaign_data.invoke({"store_id": 1})

    analysis = analysis_business_data.invoke(
        {"sales_data": sales, "campaign_data": campaign}
    )

    # 市场数据兜底：排名/客流/交易/咨询类问题，LLM 未调用新工具时确定性补查
    question = state.get("user_question", "")
    market_keys = ["排名", "综合", "哪家店", "门店对比", "客流", "交易", "咨询", "表现", "门店排行"]
    if any(k in question for k in market_keys):
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
def rag_node(state: dict) -> dict:
    """知识检索：双路检索（知识层用纯问题，避免经营结论稀释知识类查询；
    经验层带分析结论，保证历史报告相关性）。"""
    question = state.get("user_question", "")
    analysis = (state.get("analysis_result") or {}).get("data", {}) or {}
    metrics = analysis.get("metrics", {}) or {}
    factors = analysis.get("factors", []) or []

    # 知识层：纯问题检索（薪资/制度/话术类查询不被经营结论干扰）
    docs = search_operation_knowledge.invoke({"query": question, "top_k": 5})

    # 经验层：问题 + 分析结论（召回相关历史诊断报告）
    if not settings.mock_mode:
        try:
            from rag.retriever import EXPERIENCE_MAX_AGE_DAYS, get_vector_client

            exp_parts = [question]
            if metrics.get("gmv_change_pct") is not None:
                exp_parts.append(f"营业额环比{metrics['gmv_change_pct']}%")
            for f in factors[:2]:
                exp_parts.append(f.get("impact", ""))
            exp_topic = "；".join(p for p in exp_parts if p)

            exp_docs = get_vector_client().query(
                exp_topic, top_k=3, filter_type="report", max_age_days=EXPERIENCE_MAX_AGE_DAYS
            )
            for e in exp_docs:
                meta = dict(e.get("metadata") or {})
                meta["is_experience"] = True
                e["metadata"] = meta
            if exp_docs:
                docs = list(docs) + exp_docs
                logger.info("经验层命中 %s 条历史报告（max_age=%sd）", len(exp_docs), EXPERIENCE_MAX_AGE_DAYS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("经验层检索失败：%s", exc)

    return {"retrieval_docs": docs}


# ---------------------------------------------------------------- report
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


def _build_report_input(state: dict) -> str:
    """序列化 state 关键结果供报告节点使用（限制体积，防止大列表撑爆上下文）。

    - 排名/客流/交易/咨询类问题：只传市场数据（ranking/traffic/transaction/consult），
      避免单店 sales 数据干扰排名分析
    - 其他问题：传 sales + campaign（Top10）+ 知识
    - retrieval_docs 截断内容
    """
    query_result = state.get("query_result", {}) or {}
    question = state.get("user_question", "")
    market_keys = ["排名", "综合", "哪家店", "门店对比", "客流", "交易", "咨询", "表现", "门店排行"]
    is_market_question = any(k in question for k in market_keys)

    if is_market_question:
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
        }
        return json.dumps(summary, ensure_ascii=False, default=str)

    campaign = query_result.get("campaign", {}) or {}
    campaign_data = campaign.get("data", {}) if isinstance(campaign, dict) else {}
    campaigns = campaign_data.get("campaigns", []) or []
    # 保留汇总字段 + Top10（压缩成本）
    campaign_trim = dict(campaign_data)
    campaign_trim["campaigns"] = campaigns[:10]
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
            "sales": query_result.get("sales", {}),
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
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


def _ingest_report_to_kb(report: str, state: dict) -> None:
    """
    历史报告自动入库（经验层，doc_type=report，带时间元数据）。

    时间有效性设计（配合检索端 max_age_days 过滤 + 时间衰减）：
    - metadata.report_date：报告日期（检索时超期剔除/衰减）
    - metadata.period / store_id / issue_types：供按门店/异常类型过滤
    - 幂等：入库前删除当日的同类报告，避免重复提问反复累积
    """
    if not report or settings.mock_mode:
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

        doc = Document(
            page_content=report,
            metadata={
                "doc_type": "report",
                "source": "auto-generated",
                "report_date": int(date.today().strftime("%Y%m%d")),  # 整数日期（chroma $gte 仅支持数值）
                "report_date_str": today,
                "period": summary.get("period", ""),
                "store_id": str(summary.get("store_id", "")),
                "issue_types": ",".join(issue_types) or "general",
            },
        )
        chunks = split_documents([doc])
        client = get_vector_client()
        client.delete("report", report_date=today)  # 幂等：清当日旧报告
        client.add_documents(chunks, "report")
        logger.info("历史报告已入库（经验层）：%s chunks（%s）", len(chunks), today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("历史报告入库失败：%s", exc)


def report_node(state: dict) -> dict:
    """报告生成：LLM 综合数据/分析/知识生成【精炼版】报告，随后自动入库（经验层）。

    - 知识类问题（制度/手册/话术）：用知识问答 prompt（跳过经营诊断格式）
    - 其余：经营诊断 prompt
    max_tokens=3000：正文受 prompt 约束；预留推理链空间。
    兜底：推理链占满上限导致正文为空时，重试一次（不限 max_tokens），防偶发空报告。
    """
    question = state.get("user_question", "") or ""
    # 知识问答判定与 intent 保持一致：非数据类问题 → 知识问答报告（"打卡/上班时间"等词不在
    # KNOWLEDGE_KEYS 也能正确路由，避免被误判为经营诊断）
    is_knowledge = not is_data_question(question) or any(k in question for k in KNOWLEDGE_KEYS)
    system_prompt = KNOWLEDGE_REPORT_PROMPT if is_knowledge else REPORT_SYSTEM_PROMPT

    llm = create_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=_build_report_input(state)),
    ]
    # 流式调用：每个 chunk 被 LangGraph 透传 → 上层 astream(messages) 模式可逐 token 推到前端
    # 同时本地收集完整 content 供 final_report 返回（保证流结束后 state 仍有完整文本）
    parts: list[str] = []
    try:
        for chunk in llm.stream(messages, max_tokens=3000):
            content = chunk.content
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if content:
                parts.append(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("report 流式调用异常，回退非流式：%s", exc)
        response = llm.invoke(messages, max_tokens=3000)
        parts = [response.content if isinstance(response.content, str) else str(response.content)]

    report = "".join(parts).strip()

    if not report:
        logger.warning("report 首轮输出为空（推理链占满 max_tokens），重试一次")
        response = llm.invoke(messages)
        report = response.content if isinstance(response.content, str) else str(response.content)

    # 数据刷新结果前缀（确定性：不依赖 LLM 自觉提及刷新动作）
    refresh = (state.get("query_result") or {}).get("refresh_market_data") or {}
    if isinstance(refresh, dict) and refresh.get("success"):
        data = refresh.get("data") or {}
        ok_parts = [k for k, v in data.items() if isinstance(v, dict) and v.get("success")]
        report = f"📥 数据已刷新至最新（{len(ok_parts)} 项：{', '.join(ok_parts)}），可基于最新数据继续分析。\n\n{report}"

    # 经验层：报告自动入库（仅真实模式 + 经营诊断报告；知识问答报告不入经营经验层）
    if not is_knowledge:
        _ingest_report_to_kb(report, state)

    return {"final_report": report}
