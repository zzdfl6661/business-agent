"""
自动化执行工具：update_campaign_budget
=====================================
安全设计（修复"LLM 可自行 confirm=True 直接改库"的隐患）：

- **本工具永远不直接执行任何修改**，只生成待确认的执行计划（dry-run，含 plan_id）；
- 真正的执行只能通过 `POST /api/execute/confirm {plan_id}`（受 API Token 鉴权保护）触发，
  由 `tools/execution_plans.py::confirm_plan` 完成（一次性 + 10 分钟 TTL）；
- 旧的 confirm 参数已废弃并被忽略（保留仅为兼容旧 prompt 输出，绝不产生副作用）；
- 计划创建与确认均写入审计日志，全程可追溯。

[阶段二] Playwright 操作运营后台的执行钩子挂在 execution_plans.confirm_plan 的执行段。
"""
from __future__ import annotations

from langchain_core.tools import tool

from tools.execution_plans import create_plan


@tool
def update_campaign_budget(
    campaign_id: int,
    new_budget: float,
    reason: str = "",
    confirm: bool = False,
) -> dict:
    """为「调整推广预算」生成执行计划（dry-run，不修改任何数据）。

    安全机制：
    - 本工具只生成执行计划并返回 plan_id，**绝不直接修改数据**；
    - 计划需用户在界面上点击确认（或调用 POST /api/execute/confirm {"plan_id": ...}）后才会真正执行；
    - 计划一次性使用，10 分钟内有效，超期需重新生成；
    - confirm 参数已废弃并忽略（仅兼容旧调用格式），执行权始终在用户确认接口。

    参数：
        campaign_id 推广计划ID；
        new_budget  目标预算金额（必须为正数）；
        reason      调整理由（写入审计）。

    返回：{plan_id, campaign_id, old_budget, new_budget, reason, status, expires_in_seconds}。
    """
    try:
        new_budget_f = float(new_budget)
    except (TypeError, ValueError):
        return {"success": False, "data": {}, "error": f"预算必须是数字，收到 {new_budget!r}"}
    if new_budget_f <= 0:
        return {"success": False, "data": {}, "error": f"预算必须为正数，收到 {new_budget_f}"}

    result = create_plan(int(campaign_id), new_budget_f, reason or "")
    if result.get("success"):
        # 在返回给 LLM 的内容中追加执行引导（确定性：不依赖模型自觉）
        plan = result["data"]
        plan["user_instruction"] = (
            f"已生成执行计划（计划号 {plan['plan_id']}）：campaign {plan['campaign_id']} 预算 "
            f"{plan['old_budget']} → {plan['new_budget']}。请明确告知用户存在待确认的执行计划，"
            "用户点击界面上的「确认执行」按钮（或调用确认接口）后才会真正执行。"
        )
    return result
