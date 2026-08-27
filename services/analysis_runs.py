"""结构化经营分析快照：用于会话追问和通知草稿，不依赖聊天文本反解析。"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select

from database.models import AnalysisRun


def _store_id_from_state(state: dict) -> int | None:
    if state.get("store_id"):
        return int(state["store_id"])
    sales = (state.get("query_result") or {}).get("sales") or {}
    summary = (sales.get("data") or {}).get("summary") if isinstance(sales, dict) else {}
    value = (summary or {}).get("store_id")
    return int(value) if value is not None else None


def _payload_from_state(state: dict) -> dict:
    analysis = (state.get("analysis_result") or {}).get("data", {}) or {}
    sales = (state.get("query_result") or {}).get("sales", {}) or {}
    summary = (sales.get("data") or {}).get("summary", {}) if isinstance(sales, dict) else {}
    docs = state.get("retrieval_docs") or []
    return {
        "report": state.get("final_report", ""),
        "report_sections": state.get("report_sections") or {},
        "metrics": analysis.get("metrics") or {},
        "factors": analysis.get("factors") or [],
        "period": summary.get("period") or "",
        "sources": [
            (d.get("metadata") or {}).get("source", "")
            for d in docs if isinstance(d, dict)
        ][:8],
    }


def persist_analysis_run(state: dict, db_session) -> dict:
    """仅持久化经营分析结果；知识问答不创建 AnalysisRun。"""
    if state.get("intent_type") != "data":
        return {}
    payload = _payload_from_state(state)
    if not payload["report"]:
        return {}
    run_id = uuid4().hex
    row = AnalysisRun(
        run_id=run_id,
        session_id=state.get("session_id") or None,
        store_id=_store_id_from_state(state),
        question=(state.get("user_question") or "")[:4000],
        period=str(payload.get("period") or "")[:128] or None,
        payload=json.dumps(payload, ensure_ascii=False, default=str),
    )
    db_session.add(row)
    db_session.commit()
    return {"analysis_run_id": run_id, "analysis_run": payload, "store_id": row.store_id}


def load_analysis_run(run_id: str, db_session) -> dict | None:
    row = db_session.execute(select(AnalysisRun).where(AnalysisRun.run_id == run_id)).scalars().first()
    if not row:
        return None
    try:
        payload = json.loads(row.payload)
    except (TypeError, ValueError):
        payload = {}
    return {
        "analysis_run_id": row.run_id,
        "session_id": row.session_id,
        "store_id": row.store_id,
        "question": row.question,
        "period": row.period or "",
        "payload": payload,
    }


def load_latest_session_analysis(session_id: str | None, db_session) -> dict | None:
    if not session_id:
        return None
    row = db_session.execute(
        select(AnalysisRun)
        .where(AnalysisRun.session_id == session_id)
        .order_by(AnalysisRun.created_at.desc())
        .limit(1)
    ).scalars().first()
    return load_analysis_run(row.run_id, db_session) if row else None
