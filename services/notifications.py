"""通知计划状态机与确定性 dry-run Dispatcher。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from config.logging_setup import audit
from config.settings import settings
from database.models import AnalysisRun, NotificationPlan
from integrations.dingtalk_dws import DingTalkDWSHostClient, DingTalkDWSError
from integrations.dingtalk_mcp import DingTalkMCPClient, DingTalkMCPError

MAX_MESSAGE_CHARS = 1200
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_NUMBER_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?%?")


def validate_message_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise ValueError("通知内容不能为空")
    if len(value) > MAX_MESSAGE_CHARS:
        raise ValueError(f"通知内容不能超过 {MAX_MESSAGE_CHARS} 个字符")
    if _PHONE_RE.search(value):
        raise ValueError("通知内容不能包含完整手机号")
    return value


def validate_notification_facts(text: str, analysis_payload: dict, store_id: int | None = None) -> str:
    """通知中的数字只能来自分析快照；序号 1. / 2. / 3. 例外。"""
    value = validate_message_text(text)
    snapshot = json.dumps(analysis_payload or {}, ensure_ascii=False, default=str)
    allowed = set(_NUMBER_RE.findall(snapshot))
    if store_id is not None:
        allowed.add(str(store_id))
    for match in _NUMBER_RE.finditer(value):
        # 建议动作的 Markdown 序号是版式，不是经营指标。
        prefix = value[max(0, match.start() - 2):match.start()]
        suffix = value[match.end():match.end() + 2]
        if (match.start() == 0 or "\n" in prefix) and suffix.lstrip().startswith("."):
            continue
        if match.group(0) not in allowed:
            raise ValueError("通知包含无法追溯到经营分析快照的数字")
    return value


def _analysis_payload_for_plan(analysis_run_id: str, db_session) -> dict:
    row = db_session.execute(
        select(AnalysisRun).where(AnalysisRun.run_id == analysis_run_id)
    ).scalars().first()
    if not row:
        raise ValueError("经营分析快照不存在，拒绝创建通知")
    try:
        return json.loads(row.payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("经营分析快照格式无效，拒绝创建通知") from exc


def _plan_dict(plan: NotificationPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "analysis_run_id": plan.analysis_run_id,
        "session_id": plan.session_id,
        "store_id": plan.store_id,
        "intended_recipient": plan.intended_recipient,
        "effective_recipient": plan.effective_recipient,
        "message_text": plan.message_text,
        "status": plan.status,
        "result": json.loads(plan.result_detail) if plan.result_detail else None,
        "created_at": str(plan.created_at)[:19],
        "approved_at": str(plan.approved_at)[:19] if plan.approved_at else None,
        "dispatched_at": str(plan.dispatched_at)[:19] if plan.dispatched_at else None,
    }


def create_notification_plan(
    *, analysis_run_id: str, session_id: str | None, store_id: int, intended_recipient: str,
    message_text: str, db_session,
) -> dict:
    """创建待审批草稿；相同分析快照和文本复用未完成计划，避免重复通知。"""
    payload = _analysis_payload_for_plan(analysis_run_id, db_session)
    text = validate_notification_facts(message_text, payload, store_id)
    material = f"{analysis_run_id}|{store_id}|{intended_recipient}|{text}".encode("utf-8")
    idempotency_key = hashlib.sha256(material).hexdigest()
    existing = db_session.execute(
        select(NotificationPlan).where(NotificationPlan.idempotency_key == idempotency_key)
    ).scalars().first()
    if existing and existing.status in {"pending_approval", "simulated", "sent"}:
        return _plan_dict(existing)

    plan = NotificationPlan(
        plan_id=f"notice_{uuid4().hex[:24]}",
        analysis_run_id=analysis_run_id,
        session_id=session_id or None,
        store_id=store_id,
        intended_recipient=intended_recipient,
        effective_recipient=(settings.dingtalk_dry_run_recipient if settings.dingtalk_mode != "live"
                             else settings.dingtalk_live_recipient_name),
        message_text=text,
        status="pending_approval",
        idempotency_key=idempotency_key,
    )
    db_session.add(plan)
    db_session.commit()
    audit("notification_plan_created", session_id, plan_id=plan.plan_id, store_id=store_id,
          intended_recipient=intended_recipient, effective_recipient=plan.effective_recipient)
    return _plan_dict(plan)


def get_notification_plan(plan_id: str, db_session) -> NotificationPlan | None:
    return db_session.execute(
        select(NotificationPlan).where(NotificationPlan.plan_id == plan_id)
    ).scalars().first()


def list_notification_plans(db_session, status: str | None = None, limit: int = 50) -> list[dict]:
    stmt = select(NotificationPlan).order_by(NotificationPlan.created_at.desc()).limit(max(1, min(limit, 200)))
    if status:
        stmt = stmt.where(NotificationPlan.status == status)
    return [_plan_dict(row) for row in db_session.execute(stmt).scalars().all()]


def update_notification_text(plan_id: str, message_text: str, db_session) -> dict:
    plan = get_notification_plan(plan_id, db_session)
    if not plan:
        raise ValueError("通知计划不存在")
    if plan.status != "pending_approval":
        raise ValueError(f"当前状态为 {plan.status}，不能编辑")
    payload = _analysis_payload_for_plan(plan.analysis_run_id, db_session)
    plan.message_text = validate_notification_facts(message_text, payload, plan.store_id)
    db_session.commit()
    audit("notification_plan_edited", plan.session_id, plan_id=plan_id, text_len=len(plan.message_text))
    return _plan_dict(plan)


def cancel_notification_plan(plan_id: str, db_session) -> dict:
    plan = get_notification_plan(plan_id, db_session)
    if not plan:
        raise ValueError("通知计划不存在")
    if plan.status != "pending_approval":
        raise ValueError(f"当前状态为 {plan.status}，不能取消")
    plan.status = "cancelled"
    db_session.commit()
    audit("notification_plan_cancelled", plan.session_id, plan_id=plan_id)
    return _plan_dict(plan)


def confirm_notification_plan(plan_id: str, db_session) -> dict:
    """确认后默认只写 simulated；live 仅向固定的朱兴福发送。"""
    plan = get_notification_plan(plan_id, db_session)
    if not plan:
        raise ValueError("通知计划不存在")
    if plan.status in {"simulated", "sent"}:
        return _plan_dict(plan)
    if plan.status != "pending_approval":
        raise ValueError(f"当前状态为 {plan.status}，不能确认")
    payload = _analysis_payload_for_plan(plan.analysis_run_id, db_session)
    validate_notification_facts(plan.message_text, payload, plan.store_id)
    plan.approved_at = datetime.now()

    # 第一阶段以及所有未完整配置的环境都必须停在 simulated，绝不触网。
    if not settings.dingtalk_enabled or settings.dingtalk_mode != "live":
        plan.status = "simulated"
        plan.dispatched_at = datetime.now()
        plan.result_detail = json.dumps({
            "mode": "dry_run",
            "network_called": False,
            "effective_recipient": settings.dingtalk_dry_run_recipient,
            "note": "已模拟发送；钉钉 MCP 未建立连接，未产生真实外发。",
        }, ensure_ascii=False)
        db_session.commit()
        audit("notification_simulated", plan.session_id, plan_id=plan.plan_id, store_id=plan.store_id,
              intended_recipient=plan.intended_recipient, effective_recipient=plan.effective_recipient)
        return _plan_dict(plan)

    # live 当前固定只向配置中的朱兴福 userId 单聊，不按姓名或店长联系人猜测收件人。
    try:
        if settings.dingtalk_dispatcher == "dws_host":
            response = DingTalkDWSHostClient().send_text(
                settings.dingtalk_live_recipient_user_id,
                plan.message_text,
                plan.idempotency_key,
            )
        elif settings.dingtalk_dispatcher == "official_mcp":
            client = DingTalkMCPClient()
            capability = client.describe_capabilities_sync()
            if capability.get("status") != "ready":
                raise DingTalkMCPError("钉钉 MCP 所需工具不可用，拒绝真实发送")
            response = client.send_text_sync(settings.dingtalk_live_recipient_user_id, plan.message_text)
        else:
            raise ValueError("BIZ_DINGTALK_DISPATCHER 仅支持 dws_host 或 official_mcp")
    except (DingTalkDWSError, DingTalkMCPError):
        raise
    plan.status = "sent"
    plan.dispatched_at = datetime.now()
    plan.result_detail = json.dumps(
        {"mode": "live", "dispatcher": settings.dingtalk_dispatcher, "response": response},
        ensure_ascii=False,
        default=str,
    )
    db_session.commit()
    audit("notification_sent", plan.session_id, plan_id=plan.plan_id, store_id=plan.store_id,
          effective_recipient=settings.dingtalk_live_recipient_name)
    return _plan_dict(plan)
