"""
对话与知识库 API
================
- POST /api/chat       ：自然语言提问 → 经营诊断报告 + 执行轨迹(trace)
- POST /api/rag/upload ：上传知识文档（md/txt/pdf）→ 解析切分入库
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from agent.graph import agent
from config.logging_setup import audit
from database.mysql import get_session_factory
from database.models import ChatSession
from rag import loader
from rag.retriever import get_vector_client
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# 多轮对话：前端只传最近 N 轮，控制上下文体积（token 优化）
MAX_HISTORY_TURNS = 6


class HistoryItem(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="自然语言经营分析问题")
    store_id: int | None = Field(None, description="可选：指定门店 ID（1~5）")
    history: list[HistoryItem] = Field(default_factory=list, description="历史对话（兼容旧前端；新前端用 session_id）")
    session_id: str | None = Field(None, max_length=64, description="会话 ID（持久化对话上下文）")


class ChatResponse(BaseModel):
    report: str = Field(..., description="经营诊断报告（markdown）")
    trace: list[dict] = Field(default_factory=list, description="各节点执行轨迹")
    usage: dict = Field(default_factory=dict, description="token 用量统计")
    duration_ms: int = Field(0, description="本次耗时(毫秒)")


def _sum_usage(result: dict) -> dict:
    """汇总本轮 LLM 调用 token 用量（从各 AI 消息 response_metadata 提取）。"""
    input_tokens = output_tokens = 0
    for msg in result.get("messages", []) or []:
        meta = getattr(msg, "response_metadata", None) or {}
        tu = meta.get("token_usage") or meta.get("usage") or {}
        input_tokens += int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
        output_tokens += int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}


# ============ 会话持久化（轻量记忆，非 LangGraph checkpoint） ============

def _load_session_history(session_id: str | None, req_history: list) -> list[dict]:
    """加载会话历史：优先 session_id 存储，否则用请求 history（兼容）。"""
    if session_id:
        try:
            with get_session_factory()() as session:
                row = session.execute(
                    select(ChatSession).where(ChatSession.session_id == session_id)
                ).scalars().first()
            if row and row.history:
                hist = json.loads(row.history)
                if isinstance(hist, list):
                    return hist[-MAX_HISTORY_TURNS * 2:]
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载会话 %s 失败：%s", session_id, exc)
    return [{"role": h.role, "content": h.content} for h in req_history]


def _save_session_history(session_id: str, history: list[dict], report: str, question: str) -> None:
    """追加本轮问答并持久化（裁剪窗口控制体积）。"""
    if not session_id:
        return
    try:
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": report[:4000]})
        history = history[-MAX_HISTORY_TURNS * 2:]
        with get_session_factory()() as session:
            row = session.execute(
                select(ChatSession).where(ChatSession.session_id == session_id)
            ).scalars().first()
            if row:
                row.history = json.dumps(history, ensure_ascii=False)
                row.last_question = question[:500]
                row.message_count = len(history)
            else:
                session.add(ChatSession(
                    session_id=session_id,
                    history=json.dumps(history, ensure_ascii=False),
                    last_question=question[:500],
                    message_count=len(history),
                ))
            session.commit()
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


def _build_trace(result: dict) -> list[dict]:
    """从最终 state 汇总节点执行轨迹（供前端展示思考过程）。"""
    analysis = (result.get("analysis_result") or {}).get("data", {}) or {}
    metrics = analysis.get("metrics", {}) or {}
    factors = analysis.get("factors", []) or []
    sales = (result.get("query_result") or {}).get("sales") or {}
    summary = (sales.get("data") or {}).get("summary", {}) if isinstance(sales, dict) else {}
    docs = result.get("retrieval_docs") or []
    sources = {((d.get("metadata") or {}).get("source")) for d in docs if isinstance(d, dict)} - {None}

    return [
        {"node": "intent", "detail": f"意图分析完成：{str(result.get('user_question', ''))[:40]}…"},
        {
            "node": "tools",
            "detail": (
                f"数据查询：GMV={summary.get('gmv')}，环比={summary.get('gmv_change_pct')}%"
                f"（数据来源：{'模拟数据' if summary.get('is_mock') else 'MySQL'}）"
            ),
        },
        {
            "node": "analysis",
            "detail": f"归因因子：{'；'.join(f.get('impact', '') for f in factors[:2]) or '无'}",
        },
        {
            "node": "rag",
            "detail": f"知识检索 {len(docs)} 条，来源：{', '.join(sources) if sources else '内置示例知识'}",
        },
        {"node": "report", "detail": f"报告生成完成（{len(str(result.get('final_report', '')))} 字）"},
    ]


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """自然语言 → 经营诊断报告（支持 session_id 持久化 + 审计日志）。"""
    question = req.question.strip()
    if req.store_id:
        question = f"{question}\n（目标门店：{req.store_id}）"

    # 历史消息：优先 session 存储（持久化记忆），兼容请求 history
    hist = _load_session_history(req.session_id, req.history)
    from langchain_core.messages import AIMessage, HumanMessage

    messages: list[Any] = []
    for item in hist:
        msg = HumanMessage(content=item["content"]) if item["role"] == "user" else AIMessage(content=item["content"])
        messages.append(msg)
    messages.append(HumanMessage(content=question))

    t0 = time.time()

    def _run() -> dict:
        return agent.invoke({"user_question": question, "messages": messages})

    result = await run_in_threadpool(_run)
    report = result.get("final_report", "（报告生成失败，请查看日志）")
    duration_ms = int((time.time() - t0) * 1000)

    # 会话持久化 + 审计
    _save_session_history(req.session_id, hist, report, question)
    audit("chat", req.session_id, question=question[:200], report_len=len(report),
          duration_ms=duration_ms)

    return ChatResponse(
        report=report,
        trace=_build_trace(result),
        usage=_sum_usage(result),
        duration_ms=duration_ms,
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 流式对话：节点进度事件 + 报告逐 token 推送（打字机效果）。"""
    question = req.question.strip()
    if req.store_id:
        question = f"{question}\n（目标门店：{req.store_id}）"

    from langchain_core.messages import AIMessage, HumanMessage

    # 会话持久化历史（优先 session_id）
    hist = _load_session_history(req.session_id, req.history)
    messages: list[Any] = []
    for item in hist:
        msg = HumanMessage(content=item["content"]) if item["role"] == "user" else AIMessage(content=item["content"])
        messages.append(msg)
    messages.append(HumanMessage(content=question))
    inputs = {"user_question": question, "messages": messages}

    def sse(event: str, data: str) -> str:
        return f"event: {event}\ndata: {data}\n\n"

    async def gen():
        import asyncio

        from agent.graph import agent

        t0 = time.time()
        input_tokens = output_tokens = 0
        report_chars = 0
        report_text = ""
        metrics_cache: dict = {}     # Pandas 计算的核心指标（KPI 卡用）
        factors_cache: list = []     # 归因因子（KPI 卡副注用）
        seen_route = False

        # 注意：LangGraph 1.x 在 Supervisor+子图架构下，stream_mode="messages" 不会把子图内
        # report_node 的 stream chunk 透传到父图 messages 流（实测 token 事件数=0）。
        # 改用 "updates" 模式（节点级）+ final_report 就绪后用字符分块模拟打字机。
        yield sse("progress", "正在分析问题…")
        try:
            async for event in agent.astream(inputs, stream_mode="updates"):
                if not event:
                    continue
                for node_name, update in event.items():
                    if node_name == "supervisor" and not seen_route:
                        seen_route = True
                        intent_type = (update or {}).get("intent_type", "kb")
                        yield sse(
                            "progress",
                            "正在检索知识库…" if intent_type == "kb" else "正在分析数据…",
                        )
                    elif node_name in ("data_agent", "kb_agent"):
                        ana = (update or {}).get("analysis_result") or {}
                        ana_data = ana.get("data", {}) if isinstance(ana, dict) else {}
                        metrics_cache = (ana_data or {}).get("metrics", {}) or {}
                        factors_cache = (ana_data or {}).get("factors", []) or []
                        fr = (update or {}).get("final_report") or ""
                        if fr and len(fr) > len(report_text):
                            delta = fr[len(report_text):]
                            report_text = fr
                            yield sse("progress", "正在生成回答…")
                            for i in range(0, len(delta), 6):
                                sub = delta[i : i + 6]
                                report_chars += len(sub)
                                yield sse("token", sub)
                                await asyncio.sleep(0.018)
        except Exception as exc:  # noqa: BLE001
            logger.error("流式执行失败：%s", exc)
            yield sse("error", str(exc))
            return

        duration_ms = int((time.time() - t0) * 1000)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if usage["total_tokens"] == 0:
            # DeepSeek 流式不返回 usage → 按报告字符估算（实测标定，标注 estimated）
            # 真实调用：input ≈ 报告字数×12（含 prompt/工具结果/历史），output ≈ 字数×3（含推理链）
            est_in = int(report_chars * 12)
            est_out = int(report_chars * 3)
            usage = {
                "input_tokens": est_in,
                "output_tokens": est_out,
                "total_tokens": est_in + est_out,
                "estimated": True,
            }
        done = {
            "usage": usage,
            "duration_ms": duration_ms,
            "metrics": metrics_cache,
            "factors": factors_cache,
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


@router.post("/workflow/refresh")
async def workflow_refresh(req: WorkflowRefreshRequest) -> dict:
    """按钮驱动数据刷新（确定性执行，不走 LLM，避免对话触发的高成本与不可控）。"""
    from tools.data_ingest_tool import refresh_market_data

    result = await run_in_threadpool(refresh_market_data.invoke, {"datasets": req.datasets, "port": req.port})
    audit("workflow_refresh", None, datasets=req.datasets, success=result.get("success"),
          error=str(result.get("error"))[:200] if result.get("error") else None)
    return result


@router.post("/rag/upload")
async def rag_upload(file: UploadFile = File(...)) -> dict:
    """上传知识文档（md/txt/pdf）→ 解析 → 切分 → 向量化入库。"""
    filename = file.filename or "upload.md"
    dest = loader.DATA_DIR / filename
    dest.write_bytes(await file.read())

    try:
        docs = loader.load_documents([dest])
        chunks = loader.split_documents_hierarchical(docs)
        client = get_vector_client()
        # 幂等：清空该文档对应 doc_type 的旧数据
        for doc_type in {d.metadata.get("doc_type") for d in docs} - {None}:
            client.delete(doc_type)
        client.add_documents(chunks, None)
        audit("upload", None, file=filename, chunks=len(chunks),
              doc_type=list({d.metadata.get("doc_type") for d in docs} - {None}))
        return {
            "success": True,
            "file": filename,
            "chunks": len(chunks),
            "doc_type": list({d.metadata.get("doc_type") for d in docs} - {None}),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("知识文档入库失败：%s", exc)
        return {"success": False, "file": filename, "error": str(exc)}
