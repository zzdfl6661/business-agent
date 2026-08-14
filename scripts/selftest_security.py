# -*- coding: utf-8 -*-
"""自测脚本：API Token 鉴权 + 执行计划授权机制（#1 + #4）"""
import os
import sys

TEST_TOKEN = "test-token-123"
os.environ["BIZ_API_TOKEN"] = TEST_TOKEN
# 注意：不设置 BIZ_LLM_PROVIDER（沿用 .env 真实通道）；本测试不触发 LLM 对话（无 MockLLM）

# 环境缺 python-multipart 时注入假模块仅供导入 main（不涉及上传接口实测）；
# 已安装真实包（requirements 声明）则直接使用，无需注入
import types  # noqa: E402

if "python_multipart" not in sys.modules:
    try:
        import python_multipart  # noqa: F401 真实包已装 → 跳过注入
    except ImportError:
        _fake = types.ModuleType("python_multipart")
        _fake.__version__ = "0.0.99"
        sys.modules["python_multipart"] = _fake

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根目录

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("✅ " if cond else "❌ ") + name + (f" | {detail}" if detail else ""))

# ============ 1. 鉴权（TestClient） ============
from fastapi.testclient import TestClient
import main as app_module

with TestClient(app_module.app) as client:
    # 公开路径
    r = client.get("/health")
    check("公开路径 /health 200", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/")
    check("公开路径 / 200", r.status_code == 200, f"status={r.status_code}")

    # 未带 token
    r = client.get("/api/sessions")
    check("无 token /api/sessions → 401", r.status_code == 401, f"status={r.status_code}")
    # 错误 token
    r = client.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
    check("错误 token → 401", r.status_code == 401, f"status={r.status_code}")
    # X-API-Token 头
    r = client.get("/api/sessions", headers={"X-API-Token": TEST_TOKEN})
    check("X-API-Token 正确 → 200", r.status_code == 200, f"status={r.status_code}")
    # Bearer 正确
    r = client.get("/api/sessions", headers={"Authorization": "Bearer " + TEST_TOKEN})
    check("Bearer 正确 → 200", r.status_code == 200, f"status={r.status_code}")
    # 文档接口也受保护
    r = client.get("/docs")
    check("无 token /docs → 401", r.status_code == 401, f"status={r.status_code}")
    # 新执行接口受保护
    r = client.get("/api/execute/plans")
    check("无 token /api/execute/plans → 401", r.status_code == 401, f"status={r.status_code}")
    r = client.get("/api/execute/plans", headers={"Authorization": "Bearer " + TEST_TOKEN})
    check("有 token /api/execute/plans → 200", r.status_code == 200, f"status={r.status_code}, body={r.text[:120]}")

# ============ 2. 执行计划授权（真实 DB 全流程 + 现场恢复） ============
from database.mysql import get_session_factory
from database.models import Campaign
from sqlalchemy import select
from tools.execution_plans import create_plan, confirm_plan, get_plan, list_pending_plans

# 取一个真实 campaign
with get_session_factory()() as session:
    camp = session.execute(select(Campaign).limit(1)).scalars().first()
    if camp is None:
        check("测试前置：库中存在 campaign", False, "business_agent.campaigns 为空，跳过执行链路测试")
    else:
        cid, old_budget = camp.id, float(camp.budget)
        new_budget = round(old_budget + 100.0, 2)  # campaign 1 真实预算可能为 0，需保证正数

        # 2.1 生成计划（dry-run，不修改数据）
        res = create_plan(cid, new_budget, "自测：验证授权机制")
        ok = res.get("success") and res["data"].get("status") == "pending"
        pid = res["data"]["plan_id"] if ok else ""
        check("create_plan 生成 dry-run 计划", ok, f"plan_id={pid[:24]}…" if pid else res.get("error", "")[:80])

        if ok:
            # 2.2 确认后数据仍未变（dry-run 未触碰数据）
            with get_session_factory()() as session:
                after_create = float(session.get(Campaign, cid).budget)
            check("计划生成后预算未变（dry-run）", abs(after_create - old_budget) < 1e-9,
                  f"old={old_budget} after_create={after_create}")

            # 2.3 确认执行
            res = confirm_plan(pid)
            check("confirm_plan 执行成功", res.get("success"), f"status={res['data'].get('status')}")
            with get_session_factory()() as session:
                after_exec = float(session.get(Campaign, cid).budget)
            check("执行后预算已更新", abs(after_exec - new_budget) < 1e-9,
                  f"expected={new_budget} actual={after_exec}")

            # 2.4 重复确认 → 拒绝
            res = confirm_plan(pid)
            check("重复确认被拒绝（一次性）", (not res.get("success")) and "已处理" in (res.get("error") or ""),
                  res.get("error", "")[:60])

            # 2.5 恢复现场
            with get_session_factory()() as session:
                c = session.get(Campaign, cid)
                c.budget = old_budget
                session.commit()
            with get_session_factory()() as session:
                restored = float(session.get(Campaign, cid).budget)
            check("现场恢复（预算还原）", abs(restored - old_budget) < 1e-9)

        # 2.6 不存在的 campaign / plan_id
        res = create_plan(999999, 100.0, "不存在")
        check("不存在的 campaign 拒绝生成计划", not res.get("success"), res.get("error", "")[:60])
        res = confirm_plan("plan_not_exist")
        check("不存在的 plan_id 拒绝执行", not res.get("success"), res.get("error", "")[:60])

        # 2.7 待确认列表
        res = create_plan(cid, round(old_budget + 50.0, 2), "列表验证")
        if res.get("success"):
            plans = list_pending_plans()
            check("待确认计划列表包含新计划", any(p["plan_id"] == res["data"]["plan_id"] for p in plans),
                  f"pending={len(plans)}")
            confirm_plan(res["data"]["plan_id"])  # 清理
            with get_session_factory()() as session:
                c = session.get(Campaign, cid)
                c.budget = old_budget
                session.commit()

# ============ 3. 工具层：confirm 参数被忽略（不产生副作用） ============
from tools.browser_tool import update_campaign_budget
with get_session_factory()() as session:
    camp = session.execute(select(Campaign).limit(1)).scalars().first()
if camp is not None:
    res = update_campaign_budget.invoke({"campaign_id": camp.id, "new_budget": round(float(camp.budget) + 100.0, 2), "reason": "旧格式 confirm=True", "confirm": True})
    check("工具层旧 confirm=True 不再直接执行（仅生成计划）",
          res.get("success") and res["data"].get("status") == "pending" and "plan_id" in res["data"],
          f"plan_id={res['data'].get('plan_id','')[:24]}…")
    if res.get("success"):
        confirm_plan(res["data"]["plan_id"])
        with get_session_factory()() as session:
            c = session.get(Campaign, camp.id)
            c.budget = float(camp.budget)
            session.commit()
    # 非法参数
    res = update_campaign_budget.invoke({"campaign_id": camp.id, "new_budget": -5, "reason": "负数"})
    check("非法预算（负数）拒绝生成计划", not res.get("success"), res.get("error", "")[:60])

# ============ 4. 图链路：tools_node 捕获计划（不依赖 LLM） ============
from langchain_core.messages import AIMessage
from agent.nodes import _build_report_input, tools_node

cid4 = camp.id if camp is not None else 1
fake_state = {
    "messages": [AIMessage(content="", tool_calls=[{
        "name": "update_campaign_budget",
        "args": {"campaign_id": cid4, "new_budget": 888.0, "reason": "图链路测试"},
        "id": "call_test_1", "type": "tool_call",
    }])],
    "user_question": "调整1号店推广预算",
    "query_result": {},
    "pending_plans": [],
}
out = tools_node(fake_state)
plans = out.get("pending_plans") or []
check("tools_node 捕获执行计划到 state.pending_plans",
      len(plans) == 1 and plans[0].get("plan_id") and plans[0]["new_budget"] == 888.0,
      f"plans={len(plans)}")
check("tools_node 结果入 query_result（供报告/审计）",
      "update_campaign_budget" in out.get("query_result", {}),
      f"keys={sorted(out.get('query_result', {}).keys())}")
if plans:
    report_input = _build_report_input({
        "intent_type": "data",
        "user_question": "调整1号店推广预算",
        "query_result": out.get("query_result", {}),
        "analysis_result": {"data": {"metrics": {}, "factors": []}},
        "retrieval_docs": [],
        "pending_plans": plans,
    })
    check("报告输入包含 pending_plans（模型可提示用户确认）",
          f'"plan_id"' in report_input and '888.0' in report_input, report_input[:120])
    confirm_plan(plans[0]["plan_id"])  # 清理
    with get_session_factory()() as session:
        c = session.get(Campaign, cid4)
        if c is not None:
            c.budget = float(camp.budget)
            session.commit()

# 全流程对话用例不再提供（已删除 MockLLM，真实 LLM 通道需外部服务；对话链路由真实环境验证）
print()
failed = [n for n, ok, _ in results if not ok]
print(f"总用例：{len(results)}，通过：{len(results) - len(failed)}，失败：{len(failed)}")
if failed:
    print("失败项：", failed)
    sys.exit(1)
print("ALL TESTS PASSED")
