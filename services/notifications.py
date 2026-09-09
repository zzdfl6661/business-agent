"""通知计划状态机与确定性 dry-run Dispatcher。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config.logging_setup import audit
from config.request_id import get_operator_id, get_request_id
from config.settings import settings
from database.models import AnalysisRun, NotificationDispatchOutbox, NotificationPlan
from integrations.dingtalk_dws import DingTalkDWSHostClient, DingTalkDWSError
from integrations.dingtalk_mcp import DingTalkMCPClient, DingTalkMCPError

MAX_MESSAGE_CHARS = 1200
DISPATCH_LOCK_TIMEOUT = timedelta(minutes=5)
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
        "version": int(getattr(plan, "version", 1) or 1),
        "approved_by": getattr(plan, "approved_by", None),
        "cancelled_by": getattr(plan, "cancelled_by", None),
        "last_request_id": getattr(plan, "last_request_id", None),
        "result": json.loads(plan.result_detail) if getattr(plan, "result_detail", None) else None,
        "created_at": str(getattr(plan, "created_at", None))[:19],
        "approved_at": str(plan.approved_at)[:19] if getattr(plan, "approved_at", None) else None,
        "dispatched_at": str(plan.dispatched_at)[:19] if getattr(plan, "dispatched_at", None) else None,
    }


def _request_context() -> tuple[str, str]:
    return get_request_id() or "", get_operator_id() or "system"


def _dispatch_key(plan_id: str) -> str:
    """发送幂等键与草稿创建幂等键分离，且对同一 plan 永远稳定。"""
    return hashlib.sha256(f"notification-dispatch|{plan_id}".encode("utf-8")).hexdigest()


def _assert_version(plan: NotificationPlan, expected_version: int | None) -> None:
    if expected_version is None:
        return
    actual = int(getattr(plan, "version", 1) or 1)
    if actual != expected_version:
        raise ValueError(f"草稿已被其他操作修改（当前版本 {actual}），请刷新后重试")


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
        version=1,
        idempotency_key=idempotency_key,
        last_request_id=get_request_id() or None,
    )
    db_session.add(plan)
    try:
        db_session.commit()
    except IntegrityError:
        # 两个相同请求并发创建时，唯一键只允许一条；回滚后返回胜出的记录。
        db_session.rollback()
        existing = db_session.execute(
            select(NotificationPlan).where(NotificationPlan.idempotency_key == idempotency_key)
        ).scalars().first()
        if existing:
            return _plan_dict(existing)
        raise
    audit("notification_plan_created", session_id, plan_id=plan.plan_id, store_id=store_id,
          intended_recipient=intended_recipient, effective_recipient=plan.effective_recipient)
    return _plan_dict(plan)


def get_notification_plan(plan_id: str, db_session, for_update: bool = False) -> NotificationPlan | None:
    stmt = select(NotificationPlan).where(NotificationPlan.plan_id == plan_id)
    if for_update:
        stmt = stmt.with_for_update()
    return db_session.execute(stmt).scalars().first()


def list_notification_plans(db_session, status: str | None = None, limit: int = 50) -> list[dict]:
    stmt = select(NotificationPlan).order_by(NotificationPlan.created_at.desc()).limit(max(1, min(limit, 200)))
    if status:
        stmt = stmt.where(NotificationPlan.status == status)
    return [_plan_dict(row) for row in db_session.execute(stmt).scalars().all()]


def update_notification_text(
    plan_id: str, message_text: str, db_session, expected_version: int | None = None
) -> dict:
    plan = get_notification_plan(plan_id, db_session, True)
    if not plan:
        raise ValueError("通知计划不存在")
    if plan.status != "pending_approval":
        raise ValueError(f"当前状态为 {plan.status}，不能编辑")
    _assert_version(plan, expected_version)
    payload = _analysis_payload_for_plan(plan.analysis_run_id, db_session)
    plan.message_text = validate_notification_facts(message_text, payload, plan.store_id)
    old_version = int(getattr(plan, "version", 1) or 1)
    plan.version = old_version + 1
    request_id, operator_id = _request_context()
    plan.last_request_id = request_id or None
    db_session.commit()
    audit(
        "notification_plan_edited", plan.session_id, plan_id=plan_id,
        operator_id=operator_id, old_version=old_version, new_version=plan.version,
        text_len=len(plan.message_text),
    )
    return _plan_dict(plan)


def cancel_notification_plan(
    plan_id: str, db_session, expected_version: int | None = None
) -> dict:
    plan = get_notification_plan(plan_id, db_session, True)
    if not plan:
        raise ValueError("通知计划不存在")
    if plan.status != "pending_approval":
        raise ValueError(f"当前状态为 {plan.status}，不能取消")
    _assert_version(plan, expected_version)
    request_id, operator_id = _request_context()
    plan.status = "cancelled"
    plan.version = int(getattr(plan, "version", 1) or 1) + 1
    plan.cancelled_by = operator_id
    plan.last_request_id = request_id or None
    db_session.commit()
    audit(
        "notification_plan_cancelled", plan.session_id, plan_id=plan_id,
        operator_id=operator_id, version=plan.version,
    )
    return _plan_dict(plan)


def _queue_dispatch(plan: NotificationPlan, db_session) -> NotificationDispatchOutbox:
    """在审批事务内创建 Outbox；网络调用必须发生在该事务提交之后。"""
    request_id, operator_id = _request_context()
    key = getattr(plan, "dispatch_idempotency_key", None) or _dispatch_key(plan.plan_id)
    plan.dispatch_idempotency_key = key
    outbox = db_session.execute(
        select(NotificationDispatchOutbox)
        .where(NotificationDispatchOutbox.plan_id == plan.plan_id)
        .with_for_update()
    ).scalars().first()
    if outbox:
        return outbox
    outbox = NotificationDispatchOutbox(
        dispatch_id=f"dispatch_{uuid4().hex[:24]}",
        plan_id=plan.plan_id,
        idempotency_key=key,
        status="pending",
        attempts=0,
        request_id=request_id or None,
        operator_id=operator_id,
    )
    db_session.add(outbox)
    return outbox


def _dispatch_queued_notification(plan_id: str, db_session) -> dict:
    """抢占并发送一条 Outbox；DWS 失败可用相同发送键安全重试。"""
    plan = get_notification_plan(plan_id, db_session, True)
    if not plan:
        raise ValueError("通知计划不存在")
    outbox = db_session.execute(
        select(NotificationDispatchOutbox)
        .where(NotificationDispatchOutbox.plan_id == plan_id)
        .with_for_update()
    ).scalars().first()
    if not outbox:
        raise ValueError("通知发送任务不存在")
    if outbox.status == "sent" or plan.status == "sent":
        db_session.commit()
        return _plan_dict(plan)
    if outbox.status == "unknown" or plan.status == "dispatch_unknown":
        db_session.commit()
        raise ValueError("上次发送结果未知，请人工核对钉钉后再处理，系统不会自动重发")

    now = datetime.now()
    if outbox.status == "processing" and outbox.locked_at:
        if now - outbox.locked_at <= DISPATCH_LOCK_TIMEOUT:
            db_session.commit()
            return _plan_dict(plan)
        if settings.dingtalk_dispatcher == "official_mcp":
            # 官方 MCP 工具当前无业务幂等参数；进程中断后的结果无法判定，禁止盲目重发。
            outbox.status = "unknown"
            plan.status = "dispatch_unknown"
            outbox.last_error = "发送进程超时且下游不支持幂等，需人工核对"
            db_session.commit()
            raise ValueError(outbox.last_error)

    outbox.status = "processing"
    outbox.attempts = int(outbox.attempts or 0) + 1
    outbox.locked_at = now
    outbox.last_error = None
    plan.status = "dispatching"
    db_session.commit()  # 先持久化抢占状态，再做外部调用，避免长事务持有行锁。

    try:
        if settings.dingtalk_dispatcher == "dws_host":
            response = DingTalkDWSHostClient().send_text(
                settings.dingtalk_live_recipient_user_id,
                plan.message_text,
                outbox.idempotency_key,
            )
        elif settings.dingtalk_dispatcher == "official_mcp":
            client = DingTalkMCPClient()
            capability = client.describe_capabilities_sync()
            if capability.get("status") != "ready":
                raise DingTalkMCPError("钉钉 MCP 所需工具不可用，拒绝真实发送")
            response = client.send_text_sync(settings.dingtalk_live_recipient_user_id, plan.message_text)
        else:
            raise ValueError("BIZ_DINGTALK_DISPATCHER 仅支持 dws_host 或 official_mcp")
    except (DingTalkDWSError, DingTalkMCPError, ValueError) as exc:
        plan = get_notification_plan(plan_id, db_session, True)
        outbox = db_session.execute(
            select(NotificationDispatchOutbox)
            .where(NotificationDispatchOutbox.plan_id == plan_id)
            .with_for_update()
        ).scalars().first()
        if not plan or not outbox:
            db_session.rollback()
            raise
        outbox.last_error = str(exc)[:1000]
        if settings.dingtalk_dispatcher == "dws_host":
            outbox.status = "failed"
            plan.status = "dispatch_failed"
        else:
            outbox.status = "unknown"
            plan.status = "dispatch_unknown"
        plan.result_detail = json.dumps(
            {"mode": "live", "dispatcher": settings.dingtalk_dispatcher,
             "dispatch_id": outbox.dispatch_id, "error": outbox.last_error},
            ensure_ascii=False,
        )
        db_session.commit()
        audit(
            "notification_dispatch_failed", plan.session_id, plan_id=plan.plan_id,
            dispatch_id=outbox.dispatch_id, attempts=outbox.attempts,
            retryable=outbox.status == "failed", error=outbox.last_error,
        )
        raise

    plan = get_notification_plan(plan_id, db_session, True)
    outbox = db_session.execute(
        select(NotificationDispatchOutbox)
        .where(NotificationDispatchOutbox.plan_id == plan_id)
        .with_for_update()
    ).scalars().first()
    if not plan or not outbox:
        db_session.rollback()
        raise RuntimeError("发送成功但 Outbox 记录丢失，需要人工核对")
    completed_at = datetime.now()
    outbox.status = "sent"
    outbox.sent_at = completed_at
    outbox.response_detail = json.dumps(response, ensure_ascii=False, default=str)
    plan.status = "sent"
    plan.dispatched_at = completed_at
    plan.version = int(plan.version or 1) + 1
    plan.result_detail = json.dumps(
        {"mode": "live", "dispatcher": settings.dingtalk_dispatcher,
         "dispatch_id": outbox.dispatch_id, "attempts": outbox.attempts, "response": response},
        ensure_ascii=False,
        default=str,
    )
    db_session.commit()
    audit(
        "notification_sent", plan.session_id, plan_id=plan.plan_id, store_id=plan.store_id,
        dispatch_id=outbox.dispatch_id, attempts=outbox.attempts,
        effective_recipient=settings.dingtalk_live_recipient_name,
    )
    return _plan_dict(plan)


def confirm_notification_plan(
    plan_id: str, db_session, expected_version: int | None = None
) -> dict:
    """审批使用行锁和版本校验；live 先提交 Outbox，再执行外部发送。"""
    plan = get_notification_plan(plan_id, db_session, True)
    if not plan:
        raise ValueError("通知计划不存在")
    if plan.status in {"simulated", "sent"}:
        db_session.commit()
        return _plan_dict(plan)
    if plan.status in {"dispatch_pending", "dispatching", "dispatch_failed"}:
        db_session.commit()
        return _dispatch_queued_notification(plan_id, db_session)
    if plan.status == "dispatch_unknown":
        db_session.commit()
        raise ValueError("发送结果未知，请人工核对钉钉，禁止自动重发")
    if plan.status != "pending_approval":
        raise ValueError(f"当前状态为 {plan.status}，不能确认")
    _assert_version(plan, expected_version)
    payload = _analysis_payload_for_plan(plan.analysis_run_id, db_session)
    validate_notification_facts(plan.message_text, payload, plan.store_id)
    request_id, operator_id = _request_context()
    plan.approved_at = datetime.now()
    plan.approved_by = operator_id
    plan.last_request_id = request_id or None
    plan.version = int(getattr(plan, "version", 1) or 1) + 1

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
        audit(
            "notification_simulated", plan.session_id, plan_id=plan.plan_id,
            store_id=plan.store_id, operator_id=operator_id, version=plan.version,
            intended_recipient=plan.intended_recipient,
            effective_recipient=plan.effective_recipient,
        )
        return _plan_dict(plan)

    plan.status = "dispatch_pending"
    outbox = _queue_dispatch(plan, db_session)
    db_session.commit()
    audit(
        "notification_dispatch_queued", plan.session_id, plan_id=plan.plan_id,
        dispatch_id=outbox.dispatch_id, operator_id=operator_id, version=plan.version,
    )
    return _dispatch_queued_notification(plan_id, db_session)
