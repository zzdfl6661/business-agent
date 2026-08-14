"""
执行计划存储（自动化执行授权机制）
==================================
安全设计（修复"LLM 可自行 confirm=True 直接改库"的隐患）：

- `update_campaign_budget` 工具**只能生成执行计划**（dry-run），永远不直接修改数据；
- 真正的执行只能通过 `POST /api/execute/confirm {plan_id}`（受 API Token 鉴权保护）触发；
- 计划**一次性使用**（pending → executed），默认 10 分钟 TTL，超期自动作废；
- 线程安全：Agent 在 threadpool 中运行、FastAPI 事件循环并发访问，用 threading.Lock 保护；
- 确认执行时在锁内把状态预留为 executing，防止并发双击重复执行；
- 每次创建/确认都写入审计日志（execute_plan_created / execute_plan_confirmed）。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from config.logging_setup import audit

logger = logging.getLogger(__name__)

# 计划有效期（秒）：10 分钟
PLAN_TTL_SECONDS = 600
# 内存中最多保留的计划数（防无界增长）
MAX_PLANS = 200

_lock = threading.Lock()
_plans: dict[str, dict] = {}


def _new_plan_id(campaign_id: int, new_budget: float, reason: str) -> str:
    """计划号：内容 + 时间 + 随机数哈希（不可猜测、可审计溯源）。"""
    raw = f"{campaign_id}|{new_budget}|{reason}|{time.time()}|{uuid4().hex}"
    return "plan_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _cleanup_locked(now: float | None = None) -> None:
    """清理过期计划（调用方必须已持有 _lock）。"""
    now = now if now is not None else time.time()
    expired = [pid for pid, p in _plans.items() if now - p["created_at"] > PLAN_TTL_SECONDS]
    for pid in expired:
        p = _plans.pop(pid, None)
        if p:
            logger.info("执行计划 %s 已过期清除（%s → 预算 %s）", pid, p.get("campaign_id"), p.get("new_budget"))


def create_plan(campaign_id: int, new_budget: float, reason: str = "") -> dict:
    """生成"调整推广预算"执行计划（dry-run）。不修改任何数据。

    返回：{success, data:{plan_id, campaign_id, old_budget, new_budget, reason, status,
    created_at, expires_in_seconds, note}, error}。
    """
    # 纵深防御：工具层已校验，计划层再兜底一次（confirm 阶段不再重复校验）
    if new_budget is None or float(new_budget) <= 0:
        return {"success": False, "data": {}, "error": f"预算必须为正数，收到 {new_budget!r}"}

    # 校验门店推广计划存在，并读取当前预算（供展示 old_budget）
    try:
        from database.mysql import get_session_factory
        from database.models import Campaign

        with get_session_factory()() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                return {"success": False, "data": {}, "error": f"campaign {campaign_id} 不存在，无法生成执行计划"}
            old_budget = float(campaign.budget)
    except Exception as exc:  # noqa: BLE001 数据库不可用时拒绝生成（避免"假计划"）
        logger.error("生成执行计划时读取 campaign %s 失败：%s", campaign_id, exc)
        return {"success": False, "data": {}, "error": f"读取推广计划失败：{exc}"}

    now = time.time()
    plan_id = _new_plan_id(campaign_id, new_budget, reason)
    plan = {
        "plan_id": plan_id,
        "action": "update_campaign_budget",
        "campaign_id": campaign_id,
        "old_budget": old_budget,
        "new_budget": round(float(new_budget), 2),
        "reason": reason or "",
        "status": "pending",
        "created_at": now,
        "expires_in_seconds": PLAN_TTL_SECONDS,
        "note": "dry-run 执行计划：未修改任何数据。执行需用户在界面点击确认（POST /api/execute/confirm）。",
    }

    with _lock:
        _cleanup_locked(now)
        if len(_plans) >= MAX_PLANS:  # 超容量：淘汰最旧计划
            oldest = min(_plans, key=lambda k: _plans[k]["created_at"])
            _plans.pop(oldest, None)
            logger.warning("执行计划超过 %s 条，已淘汰最旧计划 %s", MAX_PLANS, oldest)
        _plans[plan_id] = plan

    audit("execute_plan_created", None, plan_id=plan_id, campaign_id=campaign_id,
          old_budget=old_budget, new_budget=plan["new_budget"], reason=reason)
    logger.info("执行计划已生成：%s（campaign %s 预算 %s → %s）", plan_id, campaign_id, old_budget, plan["new_budget"])
    return {"success": True, "data": plan, "error": None}


def get_plan(plan_id: str) -> dict | None:
    """查询计划（含过期清理副作用）。"""
    with _lock:
        _cleanup_locked()
        plan = _plans.get(plan_id)
        return dict(plan) if plan else None


def list_pending_plans() -> list[dict]:
    """列出所有待确认计划（供运营查看）。"""
    with _lock:
        _cleanup_locked()
        now = time.time()
        return [
            dict(p) for p in _plans.values()
            if p["status"] == "pending" and now - p["created_at"] <= PLAN_TTL_SECONDS
        ]


def confirm_plan(plan_id: str) -> dict:
    """确认并执行计划（一次性；TTL 内有效）。**唯一**能真正修改数据的入口。

    [阶段二] Playwright 操作运营后台的钩子应挂在本函数执行段（替代直接写库）。
    """
    with _lock:
        plan = _plans.get(plan_id)
        if plan is None:
            return {"success": False, "data": {}, "error": "执行计划不存在（可能已过期被清理），请让助手重新生成计划"}
        if plan["status"] != "pending":
            return {"success": False, "data": dict(plan), "error": f"执行计划已处理（status={plan['status']}），不能重复执行"}
        if time.time() - plan["created_at"] > PLAN_TTL_SECONDS:
            plan["status"] = "expired"
            return {"success": False, "data": dict(plan), "error": "执行计划已过期（10 分钟内有效），请让助手重新生成计划"}
        plan["status"] = "executing"  # 锁内预留，防并发双击重复执行

    # ---- 锁外执行 DB 写入（不长时间持锁） ----
    try:
        from database.mysql import get_session_factory
        from database.models import Campaign

        with get_session_factory()() as session:
            campaign = session.get(Campaign, plan["campaign_id"])
            if campaign is None:
                with _lock:
                    plan["status"] = "pending"  # 释放预留
                return {"success": False, "data": {}, "error": f"campaign {plan['campaign_id']} 不存在，执行中止"}
            old_budget = float(campaign.budget)
            campaign.budget = Decimal(str(round(plan["new_budget"], 2)))
            session.commit()
            executed_at = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        with _lock:
            plan["status"] = "pending"  # 释放预留，允许重试
        logger.error("执行计划 %s 执行失败：%s", plan_id, exc)
        return {"success": False, "data": {}, "error": f"执行失败：{exc}"}

    with _lock:
        plan["status"] = "executed"
        plan["executed_at"] = executed_at
        plan["old_budget"] = old_budget

    audit("execute_plan_confirmed", None, plan_id=plan_id, campaign_id=plan["campaign_id"],
          old_budget=old_budget, new_budget=plan["new_budget"], reason=plan.get("reason", ""))
    # #7：预算已改 → 失效数据工具缓存（campaign 查询不再返回旧预算）
    try:
        from tools.data_cache import invalidate_data_cache

        invalidate_data_cache()
    except Exception:  # noqa: BLE001
        pass
    logger.info("执行计划已确认执行：%s（campaign %s 预算 %s → %s）", plan_id, plan["campaign_id"], old_budget, plan["new_budget"])
    return {"success": True, "data": dict(plan), "error": None}
