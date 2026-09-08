"""
对话与知识库 API
================
- POST /api/chat       ：自然语言提问 → 经营诊断报告 + 执行轨迹(trace)
- POST /api/rag/upload ：上传知识文档或表格（md/txt/pdf/docx/csv/xlsx）→ 解析切分入库
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from agent.graph import agent
from config.logging_setup import audit
from config.settings import settings
from database.mysql import get_session_factory
from database.models import ChatMessage, ChatSession, SessionMemory
from services.conversation_memory import (
    build_model_context,
    normalize_memory,
    should_compact,
    summarize_memory,
)
from rag import loader
from sqlalchemy import select

logger = logging.getLogger(__name__)

_NODE_PROGRESS = {
    "supervisor": "正在识别问题和目标门店…",
    "data_agent": "正在查询营业、流量和交易数据…",
    "kb_agent": "正在检索知识库…",
    "report": "正在生成经营结论…",
    "persist_analysis": "正在保存分析快照…",
    "notification_agent": "正在生成店长沟通草稿…",
    "load_last_analysis": "正在读取刚才的分析…",
    "clarification": "正在准备澄清提示…",
}

_TOOL_PROGRESS = {
    "get_sales_data": "正在读取销售数据…",
    "get_traffic_data": "正在读取客流数据…",
    "get_transaction_data": "正在读取交易数据…",
    "get_consult_data": "正在读取咨询数据…",
    "search_operation_knowledge": "正在检索运营知识…",
}

router = APIRouter(prefix="/api", tags=["chat"])

# 数据采集全局并发锁：刷新涉及 Edge 重启/注入/下载，同一时刻只允许一个任务
_refresh_lock = asyncio.Lock()

# 兼容旧 ``chat_sessions.history`` 字段的窗口；模型上下文改由 token 预算构建。
MAX_HISTORY_TURNS = 6


class HistoryItem(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=16000)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=8000, description="自然语言经营分析问题")
    store_id: int | None = Field(None, description="可选：指定门店 ID（1~5）")
    history: list[HistoryItem] = Field(default_factory=list, description="历史对话（兼容旧前端；新前端用 session_id）")
    session_id: str | None = Field(None, max_length=64, description="会话 ID（持久化对话上下文）")


class ChatResponse(BaseModel):
    report: str = Field(..., description="经营诊断报告（markdown）")
    report_sections: dict | None = Field(None, description="#14 结构化五段报告（summary/metrics/factors/actions/risks）")
    trace: list[dict] = Field(default_factory=list, description="各节点执行轨迹")
    usage: dict = Field(default_factory=dict, description="token 用量统计")
    duration_ms: int = Field(0, description="本次耗时(毫秒)")
    pending_plans: list[dict] = Field(default_factory=list, description="已下线的旧字段，始终为空")
    analysis_run_id: str | None = Field(None, description="可复用的结构化经营分析快照 ID")
    pending_notifications: list[dict] = Field(default_factory=list, description="待审批的店长通知草稿")


class ExecuteConfirmRequest(BaseModel):
    plan_id: str = Field(..., min_length=1, max_length=64, description="已下线的旧执行计划号（仅为 410 兼容接口保留）")


class NotificationEditRequest(BaseModel):
    message_text: str = Field(..., min_length=1, max_length=1200)


def _sum_usage(result: dict) -> dict:
    """汇总本轮 LLM 调用 token 用量（从各 AI 消息 response_metadata 提取）。"""
    input_tokens = output_tokens = 0
    for msg in result.get("messages", []) or []:
        meta = getattr(msg, "response_metadata", None) or {}
        tu = meta.get("token_usage") or meta.get("usage") or {}
        input_tokens += int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
        output_tokens += int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}


# ============ 会话持久化与结构化上下文 ============

def _legacy_history(row: ChatSession | None) -> list[dict]:
    if not row or not row.history:
        return []
    try:
        value = json.loads(row.history)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _load_session_context(session_id: str | None, req_history: list) -> tuple[list[dict], dict, list]:
    """加载完整原文，并构造预算内的模型上下文；摘要永不取代原文存档。"""
    raw_messages = [{"sequence": i + 1, "role": h.role, "content": h.content}
                    for i, h in enumerate(req_history)]
    memory: dict = {}
    if session_id:
        try:
            with get_session_factory()() as session:
                rows = session.execute(
                    select(ChatMessage).where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.sequence.asc())
                ).scalars().all()
                legacy = session.execute(
                    select(ChatSession).where(ChatSession.session_id == session_id)
                ).scalars().first()
                memory_row = session.execute(
                    select(SessionMemory).where(SessionMemory.session_id == session_id)
                ).scalars().first()
            if rows:
                raw_messages = [{"sequence": r.sequence, "role": r.role, "content": r.content}
                                for r in rows]
            else:
                raw_messages = [{"sequence": i + 1, "role": item.get("role", "user"),
                                 "content": str(item.get("content", ""))}
                                for i, item in enumerate(_legacy_history(legacy))]
            memory = normalize_memory(memory_row.memory if memory_row else {}, settings.conversation_memory_max_chars)
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载会话 %s 失败：%s", session_id, exc)
    context = build_model_context(
        raw_messages, memory,
        token_budget=settings.conversation_context_token_budget,
        recent_turns=settings.conversation_recent_turns,
        memory_max_chars=settings.conversation_memory_max_chars,
    )
    return raw_messages, memory, context


def _compact_session_memory(session_id: str) -> None:
    """对较早原文做增量摘要。失败不推进覆盖位置，因此下次可安全重试。"""
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(ChatMessage).where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.sequence.asc())
            ).scalars().all()
            memory_row = session.execute(
                select(SessionMemory).where(SessionMemory.session_id == session_id)
            ).scalars().first()
            raw = [{"sequence": r.sequence, "role": r.role, "content": r.content} for r in rows]
            existing = normalize_memory(memory_row.memory if memory_row else {}, settings.conversation_memory_max_chars)
            covered = memory_row.covered_through_sequence if memory_row else 0
        to_compact = should_compact(
            raw, covered,
            threshold_tokens=settings.conversation_compact_threshold_tokens,
            recent_turns=settings.conversation_recent_turns,
        )
        if not to_compact:
            return
        updated = summarize_memory(existing, to_compact, max_chars=settings.conversation_memory_max_chars)
        if updated is None:
            logger.warning("会话 %s 摘要失败，保留原文并将在后续轮次重试", session_id)
            return
        with get_session_factory()() as session:
            memory_row = session.execute(
                select(SessionMemory).where(SessionMemory.session_id == session_id)
            ).scalars().first()
            if memory_row:
                memory_row.memory = json.dumps(updated, ensure_ascii=False)
                memory_row.covered_through_sequence = int(to_compact[-1]["sequence"])
                memory_row.version += 1
            else:
                session.add(SessionMemory(
                    session_id=session_id, memory=json.dumps(updated, ensure_ascii=False),
                    covered_through_sequence=int(to_compact[-1]["sequence"]), version=1,
                ))
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("压缩会话 %s 失败：%s", session_id, exc)


def _save_session_history(session_id: str, history: list[dict], report: str, question: str) -> None:
    """完整追加原始问答；旧 history 字段仅保留兼容窗口，绝非历史唯一来源。"""
    if not session_id:
        return
    try:
        with get_session_factory()() as session:
            row = session.execute(select(ChatSession).where(ChatSession.session_id == session_id)).scalars().first()
            existing = session.execute(
                select(ChatMessage).where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.sequence.asc())
            ).scalars().all()
            # 首次升级时，把旧短窗口迁入原始消息表，之后所有新消息都完整保存。
            if not existing:
                for index, item in enumerate(history, start=1):
                    session.add(ChatMessage(session_id=session_id, sequence=index,
                                            role=item.get("role", "user"), content=str(item.get("content", ""))))
                next_sequence = len(history) + 1
            else:
                next_sequence = int(existing[-1].sequence) + 1
            session.add_all([
                ChatMessage(session_id=session_id, sequence=next_sequence, role="user", content=question),
                ChatMessage(session_id=session_id, sequence=next_sequence + 1, role="assistant", content=report),
            ])
            legacy = [*history, {"role": "user", "content": question},
                      {"role": "assistant", "content": report[:4000]}][-MAX_HISTORY_TURNS * 2:]
            if row:
                row.history = json.dumps(legacy, ensure_ascii=False)
                row.last_question = question[:500]
                row.message_count = next_sequence + 1
            else:
                session.add(ChatSession(session_id=session_id, history=json.dumps(legacy, ensure_ascii=False),
                                        last_question=question[:500], message_count=next_sequence + 1))
            session.commit()
        _compact_session_memory(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存会话 %s 失败：%s", session_id, exc)


@router.get("/sessions")
def list_sessions(limit: int = 20) -> dict:
    """历史会话列表（供前端恢复对话）。"""
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
            ).scalars().all()
        return {"success": True, "sessions": [
            {"session_id": r.session_id, "last_question": r.last_question,
             "message_count": r.message_count, "updated_at": str(r.updated_at)[:19]}
            for r in rows
        ]}
    except Exception as exc:  # noqa: BLE001
        logger.error("会话列表失败：%s", exc)
        return {"success": False, "sessions": []}


@router.post("/sessions/{session_id}")
def create_or_get_session(session_id: str) -> dict:
    """确保会话存在（幂等）。"""
    if not session_id:
        return {"success": False}
    try:
        with get_session_factory()() as session:
            row = session.execute(
                select(ChatSession).where(ChatSession.session_id == session_id)
            ).scalars().first()
            if not row:
                session.add(ChatSession(session_id=session_id, history="[]"))
                session.commit()
        return {"success": True, "session_id": session_id}
    except Exception as exc:  # noqa: BLE001
        logger.error("创建会话失败：%s", exc)
        return {"success": False}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    """删除会话（#15-② 前端会话管理）。"""
    if not session_id:
        return {"success": False, "error": "session_id 为空"}
    try:
        with get_session_factory()() as session:
            row = session.execute(
                select(ChatSession).where(ChatSession.session_id == session_id)
            ).scalars().first()
            if not row:
                return {"success": False, "error": "会话不存在"}
            for model in (ChatMessage, SessionMemory):
                for related in session.execute(select(model).where(model.session_id == session_id)).scalars().all():
                    session.delete(related)
            session.delete(row)
            session.commit()
        audit("session_delete", session_id)
        return {"success": True, "session_id": session_id}
    except Exception as exc:  # noqa: BLE001
        logger.error("删除会话失败：%s", exc)
        return {"success": False, "error": str(exc)[:120]}


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str) -> dict:
    """取单个会话的历史消息（前端切换会话时渲染历史、继续对话）。"""
    try:
        with get_session_factory()() as session:
            row = session.execute(
                select(ChatSession).where(ChatSession.session_id == session_id)
            ).scalars().first()
            raw_rows = session.execute(
                select(ChatMessage).where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.sequence.asc())
            ).scalars().all()
        if not row:
            return {"success": False, "error": "会话不存在"}
        messages = ([{"role": item.role, "content": item.content} for item in raw_rows]
                    if raw_rows else _legacy_history(row))
        return {
            "success": True,
            "session_id": session_id,
            "messages": messages,          # [{role, content}, ...]
            "last_question": row.last_question,
            "message_count": row.message_count,
            "updated_at": str(row.updated_at)[:19],
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("取会话 %s 消息失败：%s", session_id, exc)
        return {"success": False, "error": str(exc)[:120]}


def _build_trace(result: dict) -> list[dict]:
    """从最终 state 汇总节点执行轨迹（供前端展示思考过程）。

    #15-④ 修复：kb 问题不再硬编码"数据查询：GMV=…"，按真实 intent_type/query_result 推导。
    """
    analysis = (result.get("analysis_result") or {}).get("data", {}) or {}
    metrics = analysis.get("metrics", {}) or {}
    factors = analysis.get("factors", []) or []
    sales = (result.get("query_result") or {}).get("sales") or {}
    summary = (sales.get("data") or {}).get("summary", {}) if isinstance(sales, dict) else {}
    docs = result.get("retrieval_docs") or []
    sources = {((d.get("metadata") or {}).get("source")) for d in docs if isinstance(d, dict)} - {None}
    intent_type = result.get("intent_type", "kb")

    if intent_type == "kb":
        tools_detail = "知识问答链路：未调用数据工具，直接检索知识库"
    else:
        qr = result.get("query_result") or {}
        tools_used = [
            k for k, v in qr.items()
            if v and isinstance(v, dict) and v.get("success") is not False
        ]
        if summary.get("gmv") is not None:
            tools_detail = (
                f"数据查询：GMV={summary.get('gmv')}，环比={summary.get('gmv_change_pct')}%"
                f"（数据来源：{summary.get('data_source') or 'MySQL'}）"
            )
        elif tools_used:
            tools_detail = f"工具查询：{', '.join(tools_used)}（数据来源：MySQL）"
        else:
            tools_detail = "未调用数据工具"

    return [
        {"node": "intent", "detail": f"意图分析完成：{str(result.get('user_question', ''))[:40]}…"},
        {"node": "tools", "detail": tools_detail},
        {
            "node": "analysis",
            "detail": f"归因因子：{'；'.join(f.get('impact', '') for f in factors[:2]) or '无'}",
        },
        {
            "node": "rag",
            "detail": f"知识检索 {len(docs)} 条，来源：{', '.join(sources) if sources else '知识库未命中'}",
        },
        {"node": "report", "detail": f"报告生成完成（{len(str(result.get('final_report', '')))} 字）"},
    ]


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """自然语言 → 经营诊断报告（支持 session_id 持久化 + 审计日志）。"""
    # #8：request_id 贯穿全链路（日志 formatter + 审计 payload + 前端 trace）
    from config.request_id import new_request_id, set_request_id

    request_id = new_request_id()
    set_request_id(request_id)

    question = req.question.strip()
    if req.store_id:
        question = f"{question}\n（目标门店：{req.store_id}）"

    # 历史消息：优先 session 存储（持久化记忆），兼容请求 history
    hist, session_memory, messages = _load_session_context(req.session_id, req.history)
    from langchain_core.messages import AIMessage, HumanMessage

    messages.append(HumanMessage(content=question))

    t0 = time.time()

    def _run() -> dict:
        return agent.invoke({"user_question": question, "messages": messages,
                             "session_id": req.session_id or "", "session_memory": session_memory})

    result = await run_in_threadpool(_run)
    report = result.get("final_report", "（报告生成失败，请查看日志）")
    duration_ms = int((time.time() - t0) * 1000)

    # 会话持久化 + 审计
    _save_session_history(req.session_id, hist, report, question)
    audit("chat", req.session_id, question=question[:200], report_len=len(report),
          duration_ms=duration_ms)

    return ChatResponse(
        report=report,
        report_sections=result.get("report_sections") or None,
        trace=_build_trace(result),
        usage=_sum_usage(result),
        duration_ms=duration_ms,
        pending_plans=[],  # 预算修改执行功能已下线，保留字段兼容旧客户端
        analysis_run_id=result.get("analysis_run_id") or None,
        pending_notifications=result.get("pending_notifications") or [],
    )


# ============ 审计查询（#8：运营者查"谁做了什么"） ============

@router.get("/audit")
def query_audit(
    event_type: str | None = None,
    session_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> dict:
    """只读审计查询：按时间 / 事件类型 / 会话过滤（受 API Token 鉴权保护）。

    参数：event_type（chat/tool_call/upload/execute_plan_created/…）；
    start/end（YYYY-MM-DD）；session_id；limit（默认 100，上限 500）。
    """
    from datetime import datetime

    from sqlalchemy import and_, desc

    from database.models import AuditLog

    limit = max(1, min(int(limit or 100), 500))
    try:
        with get_session_factory()() as session:
            stmt = select(AuditLog)
            conds = []
            if event_type:
                conds.append(AuditLog.event_type == event_type)
            if session_id:
                conds.append(AuditLog.session_id == session_id)
            if start:
                conds.append(AuditLog.created_at >= datetime.fromisoformat(start))
            if end:
                # end 视为含当日：end 23:59:59.999999
                conds.append(AuditLog.created_at <= datetime.fromisoformat(end).replace(hour=23, minute=59, second=59, microsecond=999999))
            if conds:
                stmt = stmt.where(and_(*conds))
            rows = session.execute(
                stmt.order_by(desc(AuditLog.created_at)).limit(limit)
            ).scalars().all()
        return {
            "success": True,
            "total": len(rows),
            "items": [
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "session_id": r.session_id,
                    "created_at": str(r.created_at)[:19],
                    "detail": _safe_detail(r.detail),
                }
                for r in rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("审计查询失败：%s", exc)
        return {"success": False, "error": f"审计查询失败（参数格式？）：{str(exc)[:150]}"}


def _safe_detail(detail: str | None) -> dict:
    """审计 detail JSON 解析（防脏数据；脱敏敏感字段）。"""
    if not detail:
        return {}
    try:
        d = json.loads(detail)
        if isinstance(d, dict):
            from tools.sanitize import sanitize_error

            for k in list(d.keys()):
                if any(s in str(k).lower() for s in ("password", "token", "key", "secret")):
                    d[k] = "***"
            return d
        return {"raw": str(d)[:300]}
    except (TypeError, ValueError):
        return {"raw": str(detail)[:300]}


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 流式对话：节点进度事件 + 报告逐 token 推送（打字机效果）。"""
    # #8：request_id 贯穿（SSE done 事件回传，前端 trace 可展示）
    from config.request_id import get_request_id, new_request_id, set_request_id

    request_id = new_request_id()
    set_request_id(request_id)

    question = req.question.strip()
    if req.store_id:
        question = f"{question}\n（目标门店：{req.store_id}）"

    from langchain_core.messages import AIMessage, HumanMessage

    # 会话持久化历史（优先 session_id）
    hist, session_memory, messages = _load_session_context(req.session_id, req.history)
    messages.append(HumanMessage(content=question))
    inputs = {"user_question": question, "messages": messages,
              "session_id": req.session_id or "", "session_memory": session_memory}

    def sse(event: str, data: str) -> str:
        return f"event: {event}\ndata: {data}\n\n"

    async def gen():
        import asyncio

        t0 = time.time()
        input_tokens = output_tokens = 0
        report_chars = 0
        report_text = ""
        report_sections: dict | None = None   # #14 结构化五段（data 链路）
        metrics_cache: dict = {}     # Pandas 计算的核心指标（KPI 卡用）
        factors_cache: list = []     # 归因因子（KPI 卡副注用）
        pending_plans: list = []     # 兼容旧字段；预算执行已下线
        pending_notifications: list = []
        analysis_run_id: str | None = None
        seen_route = False
        intent_type = ""             # 路由方向（supervisor 事件确定后用于过滤 token 透传）
        saw_report_start = False

        # #5 真流式：astream_events 透传子图内 report_node 的 LLM token（不再 updates 模式 +
        # 模拟打字机）。事件按 metadata.langgraph_node 过滤：
        # - "report" 节点的 on_chat_model_stream → 逐 token 推送到前端（真流式）
        # - on_chat_model_end → 采集真实 usage（不再按字数粗估）
        # - supervisor/data_agent/kb_agent 的 on_chain_end → 节点进度 + KPI 结构化数据
        yield sse("progress", "正在分析问题…")
        try:
            async for event in agent.astream_events(inputs, version="v2"):
                kind = event.get("event", "")
                meta = event.get("metadata") or {}
                node = meta.get("langgraph_node", "")

                if kind == "on_chain_start" and node in _NODE_PROGRESS:
                    yield sse("progress", _NODE_PROGRESS[node])
                elif kind == "on_tool_start":
                    tool_progress = _TOOL_PROGRESS.get(event.get("name", ""))
                    if tool_progress:
                        yield sse("progress", tool_progress)

                if kind == "on_chain_end" and node == "supervisor" and not seen_route:
                    # 注意：supervisor 会触发两次 on_chain_end——第一次是条件边解析事件
                    # （output=路由目标字符串如 "data"），第二次才是节点真实输出 dict
                    # （{"intent_type": ..., "store_id": ...}）。仅在 dict 时采用。
                    out = event.get("data", {}).get("output")
                    if isinstance(out, dict) and out.get("intent_type") in ("data", "kb"):
                        seen_route = True
                        intent_type = out["intent_type"]
                        yield sse(
                            "progress",
                            "正在检索知识库…" if intent_type == "kb" else "正在分析数据…",
                        )

                elif kind == "on_chain_end" and node in (
                    "data_agent", "kb_agent", "persist_analysis", "notification_agent", "clarification"
                ):
                    out = event.get("data", {}).get("output", {}) or {}
                    if isinstance(out, dict):
                        ana = out.get("analysis_result") or {}
                        ana_data = ana.get("data", {}) if isinstance(ana, dict) else {}
                        if ana_data.get("metrics"):
                            metrics_cache = (ana_data or {}).get("metrics", {}) or {}
                        if ana_data.get("factors"):
                            factors_cache = (ana_data or {}).get("factors", []) or []
                        if out.get("analysis_run_id"):
                            analysis_run_id = out["analysis_run_id"]
                        for plan in out.get("pending_notifications") or []:
                            if isinstance(plan, dict) and plan.get("plan_id") and \
                                    all(x.get("plan_id") != plan["plan_id"] for x in pending_notifications):
                                pending_notifications.append(plan)
                        fr = out.get("final_report") or ""
                        if fr and len(fr) > len(report_text):
                            report_text = fr
                        # #14：结构化五段（data 链路，report_node 返回后进入子图 state）
                        if out.get("report_sections"):
                            report_sections = out["report_sections"]

                elif kind == "on_chat_model_stream" and node == "report":
                    # #5 真流式：仅 kb 链路透传逐 token（口语化回答）；
                    # data 链路为结构化 JSON（invoke 收集），逐字透传 JSON 会污染打字机，
                    # 改由 done 事件的 report_sections 一次性渲染。
                    if intent_type == "data":
                        continue
                    chunk = event.get("data", {}).get("chunk")
                    content = getattr(chunk, "content", "") if chunk else ""
                    if isinstance(content, list):
                        content = "".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if content:
                        if not saw_report_start:
                            saw_report_start = True
                            yield sse("progress", "正在生成回答…")
                        report_text += content
                        report_chars += len(content)
                        yield sse("token", content)

                elif kind == "on_chat_model_end":
                    output = event.get("data", {}).get("output")
                    resp_meta = getattr(output, "response_metadata", None) or {}
                    tu = resp_meta.get("token_usage") or resp_meta.get("usage") or {}
                    input_tokens += int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                    output_tokens += int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.error("流式执行失败：%s", exc, exc_info=True)
            from tools.sanitize import sanitize_error

            yield sse("error", sanitize_error(str(exc)))  # #8 脱敏
            yield "retry: 3000\n\n"  # SSE 重连提示（#8）
            return

        duration_ms = int((time.time() - t0) * 1000)
        # 兜底：结构化链路无 token 事件且子图 end 未回传 final_report 时，用 sections 确定性生成文本
        if not report_text and report_sections:
            from agent.nodes import _sections_to_markdown

            report_text = _sections_to_markdown(report_sections)
        # data 链路无 token 事件 → 按最终报告长度估算 usage（避免显示 0）
        if report_chars == 0 and report_text:
            report_chars = len(report_text)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if usage["total_tokens"] == 0:
            # 流式通道不返回 usage 时按报告字符估算（保留旧逻辑，标注 estimated）
            est_in = int(report_chars * 12)
            est_out = int(report_chars * 3)
            usage = {
                "input_tokens": est_in,
                "output_tokens": est_out,
                "total_tokens": est_in + est_out,
                "estimated": True,
            }
        done = {
            "request_id": request_id,      # #8 前端 trace 展示
            "usage": usage,
            "duration_ms": duration_ms,
            "metrics": metrics_cache,
            "factors": factors_cache,
            "report_sections": report_sections,  # #14 结构化五段（data 链路；kb 为 None）
            "report": report_text,         # 完整报告 markdown（data 结构化链路无 token 事件，供前端回填 history/导出）
            "pending_plans": pending_plans,
            "analysis_run_id": analysis_run_id,
            "pending_notifications": pending_notifications,
            "user_question": question,   # 前端按问题筛 KPI 卡
        }
        yield sse("done", json.dumps(done, ensure_ascii=False))

        # 会话持久化 + 审计（流式结束后）
        _save_session_history(req.session_id, hist, report_text, question)
        audit("chat", req.session_id, question=question[:200],
              report_len=len(report_text), duration_ms=duration_ms, stream=True)

    from fastapi.responses import StreamingResponse

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class WorkflowRefreshRequest(BaseModel):
    datasets: str = Field("campaign", description="要刷新的数据集：campaign/traffic/transaction/consult/all 或逗号组合")
    port: int = Field(9222, description="Edge 调试端口")


# ============ 旧预算执行接口（已下线，保留兼容提示） ============

@router.post("/execute/confirm")
def execute_confirm(req: ExecuteConfirmRequest) -> dict:
    """已弃用：预算修改能力替换为店长通知授权流，绝不再写业务数据。"""
    raise HTTPException(status_code=410, detail="推广预算直接修改已下线；请使用店长通知草稿的确认接口。")


@router.get("/execute/plans")
def list_execute_plans() -> dict:
    raise HTTPException(status_code=410, detail="推广预算执行计划已下线")


@router.get("/notifications")
def notifications_list(status: str | None = None, limit: int = 50) -> dict:
    from services.notifications import list_notification_plans

    with get_session_factory()() as session:
        return {"success": True, "plans": list_notification_plans(session, status=status, limit=limit)}


@router.patch("/notifications/{plan_id}")
def notifications_edit(plan_id: str, req: NotificationEditRequest) -> dict:
    from services.notifications import update_notification_text

    try:
        with get_session_factory()() as session:
            return {"success": True, "plan": update_notification_text(plan_id, req.message_text, session)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}


@router.post("/notifications/{plan_id}/confirm")
def notifications_confirm(plan_id: str) -> dict:
    from integrations.dingtalk_dws import DingTalkDWSError
    from integrations.dingtalk_mcp import DingTalkMCPError
    from services.notifications import confirm_notification_plan

    try:
        with get_session_factory()() as session:
            return {"success": True, "plan": confirm_notification_plan(plan_id, session)}
    except (ValueError, DingTalkDWSError, DingTalkMCPError) as exc:
        return {"success": False, "error": str(exc)}


@router.post("/notifications/{plan_id}/cancel")
def notifications_cancel(plan_id: str) -> dict:
    from services.notifications import cancel_notification_plan

    try:
        with get_session_factory()() as session:
            return {"success": True, "plan": cancel_notification_plan(plan_id, session)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}


# ============ LLM provider 运行时切换（#15） ============

class LLMSwitchRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32, description="目标 provider：deepseek/openai_compatible/local/codebuddy")


@router.get("/llm/providers")
def llm_providers() -> dict:
    """当前 LLM 通道状态（供前端下拉渲染）。"""
    from config.llm_factory import list_providers

    return {"success": True, **list_providers()}


@router.post("/llm/switch")
def llm_switch(req: LLMSwitchRequest) -> dict:
    """运行时切换 LLM provider（内存级，无需改 .env 重启；#15）。

    切换前立即校验：目标通道未配置 Key 会返回明确错误（前端可提示）。
    """
    from config.llm_factory import list_providers, set_active_provider

    try:
        active = set_active_provider(req.provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 切换失败：%s", exc)
        return {"success": False, "error": str(exc), **list_providers()}
    audit("llm_switch", None, provider=req.provider)
    return {"success": True, "active": active, **list_providers()}


@router.post("/workflow/refresh")
async def workflow_refresh(req: WorkflowRefreshRequest) -> dict:
    """按钮驱动数据刷新（async 直调）。

    全局并发锁：刷新涉及 Edge 重启/登录态注入/下载，**同一时刻只允许一个任务**。
    多标签页/快速连点会触发并发请求互相 kill Edge 导致注入失败（用户侧反复失败根因）。
    """
    if _refresh_lock.locked():
        return {"success": False, "data": {}, "error": "已有刷新任务进行中，请等待完成后重试"}
    async with _refresh_lock:
        try:
            from config.settings import settings

            if settings.collector_url:
                import httpx

                headers = {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else {}
                async with httpx.AsyncClient(timeout=660.0) as client:
                    response = await client.post(
                        f"{settings.collector_url.rstrip('/')}/api/collector/refresh",
                        json={"datasets": req.datasets, "port": req.port},
                        headers=headers,
                    )
                    response.raise_for_status()
                    result = response.json()
            else:
                from tools.data_ingest_tool import refresh_market_data

                result = await refresh_market_data(req.datasets, req.port)
        except Exception as exc:  # noqa: BLE001
            logger.error("workflow_refresh 未捕获异常：%s", exc, exc_info=True)
            result = {
                "success": False,
                "data": {},
                "error": f"采集器不可用或刷新失败：{str(exc)[:150]}",
            }
    if result.get("success"):
        # #7：刷新成功后立即失效数据工具缓存，保证下一次问答拿到最新数据
        from tools.data_cache import invalidate_data_cache

        invalidate_data_cache()
    audit("workflow_refresh", None, datasets=req.datasets, success=result.get("success"),
          error=str(result.get("error"))[:200] if result.get("error") else None)
    return result


@router.get("/rag/documents")
def rag_documents() -> dict:
    """知识库管理页所需的文件清单、上传限制与父子切割配置。"""
    return {"success": True, **loader.list_knowledge_documents()}


@router.post("/rag/upload")
async def rag_upload(file: UploadFile = File(...)) -> dict:
    """上传知识文档或表格（md/txt/pdf/docx/csv/xlsx）→ 解析 → 切分 → 向量化入库。

    安全（#2）：文件名取 basename（防路径穿越）、白名单后缀、≤20MB、内容非空；
    幂等：仅清理**同名文件**的旧数据，不再按 doc_type 清库（修复误删其他 general 文档）。
    """
    content = await file.read()
    try:
        # 解析、向量化和 Chroma 写入均为阻塞型工作，放入线程池避免阻塞其他 SSE 对话。
        result = await run_in_threadpool(loader.upload_and_ingest, file.filename or "", content)
    except Exception as exc:  # noqa: BLE001
        logger.error("知识文档入库失败：%s", exc)
        return {"success": False, "file": file.filename or "", "error": str(exc)}
    audit("upload", None, file=result["file"], chunks=result["chunks"], doc_type=result["doc_type"])
    return result
