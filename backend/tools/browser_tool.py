"""
自动化执行工具：update_campaign_budget
=====================================
安全设计：
- LLM 不能直接操作浏览器 → 一切执行经本工具（Agent → Tool → Playwright）
- confirm=False（默认）：dry-run，只返回执行计划，不产生任何副作用
- confirm=True：真正执行（阶段一写库更新预算；阶段二接入 Playwright 操作运营后台）

阶段二 Playwright 脚本骨架见 _execute_with_playwright（TODO）。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from langchain_core.tools import tool

from config.settings import settings
from database.models import Campaign
from database.mysql import get_session_factory

logger = logging.getLogger(__name__)


def _execute_with_playwright(campaign_id: int, new_budget: float) -> dict:
    """
    [阶段二] 用 Playwright 模拟运营后台操作：
    1. 打开 BIZ_OPS_PLATFORM_URL，登录
    2. 定位推广计划列表 → 目标 campaign → 预算输入框
    3. 填入 new_budget → 保存
    目前仅提供骨架，待阶段二实现。
    """
    raise NotImplementedError(
        f"Playwright 自动化执行属第二阶段：campaign_id={campaign_id} new_budget={new_budget}，"
        f"目标平台 {settings.ops_platform_url}"
    )


@tool
def update_campaign_budget(
    campaign_id: int,
    new_budget: float,
    reason: str = "",
    confirm: bool = False,
) -> dict:
    """调整推广计划预算（自动化执行，需授权）。

    参数：
        campaign_id 推广计划ID；
        new_budget  新预算金额；
        reason      调整理由（写入审计）；
        confirm     是否确认执行。False 时仅返回执行计划（dry-run），不产生任何修改。

    返回：{dry_run: bool, campaign_id, old_budget, new_budget, reason, executed}。
    """
    # ---- dry-run：仅返回计划 ----
    if not confirm:
        return {
            "success": True,
            "data": {
                "dry_run": True,
                "campaign_id": campaign_id,
                "new_budget": new_budget,
                "reason": reason,
                "plan": f"将 campaign {campaign_id} 预算调整为 {new_budget}（原因：{reason or '未填写'}）",
                "note": "dry-run 模式：未产生任何修改；如需执行请将 confirm=True（需用户授权）",
            },
            "error": None,
        }

    # ---- 执行：阶段一写库（阶段二改由 Playwright 操作运营后台） ----
    try:
        with get_session_factory()() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                return {"success": False, "data": {}, "error": f"campaign {campaign_id} 不存在"}
            old_budget = float(campaign.budget)
            campaign.budget = Decimal(str(round(new_budget, 2)))
            session.commit()
            logger.info("已执行预算调整：campaign %s 预算 %s → %s（原因：%s）", campaign_id, old_budget, new_budget, reason)
            return {
                "success": True,
                "data": {
                    "dry_run": False,
                    "executed": True,
                    "campaign_id": campaign_id,
                    "old_budget": old_budget,
                    "new_budget": new_budget,
                    "reason": reason,
                    "note": "预算已更新至数据库；阶段二将改为 Playwright 操作运营后台",
                },
                "error": None,
            }
    except Exception as exc:
        logger.error("预算调整失败：%s", exc)
        return {"success": False, "data": {}, "error": f"预算调整执行失败：{exc}"}
