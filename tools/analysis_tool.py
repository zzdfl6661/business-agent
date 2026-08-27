"""
数据分析工具：analysis_business_data（Pandas 确定性计算）
=========================================================
输入：get_sales_data / get_campaign_data 的输出结构。
计算：GMV 变化、环比增长、客单价、推广转化率、ROI、品类贡献度、异常检测。
原则：指标计算全部由 Pandas 确定性完成，LLM 不参与算术，杜绝"幻觉算数"。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _pct(cur: float, prev: float) -> float | None:
    return round((cur - prev) / prev * 100, 2) if prev else None


@tool
def analysis_business_data(
    sales_data: dict | None = None,
    campaign_data: dict | None = None,
    traffic_data: dict | None = None,
    transaction_data: dict | None = None,
    consult_data: dict | None = None,
    dimension: str = "day",
) -> dict:
    """对销售、推广、客流、交易和咨询数据做确定性指标计算与归因。

    参数：
        sales_data     get_sales_data 的返回 data 部分；
        campaign_data  get_campaign_data 的返回 data 部分（可选）；
        dimension      聚合维度（day/category，默认 day）。

    返回：{metrics, dimension_breakdown, anomalies, factors}。
    """
    sales = (sales_data or {}).get("data", {}) if isinstance(sales_data, dict) and "data" in sales_data else (sales_data or {})
    summary = sales.get("summary", {}) or {}
    daily = sales.get("daily", []) or []
    category = sales.get("category_breakdown", []) or []
    top_products = sales.get("top_products", []) or []

    # ---------------- 核心指标 ----------------
    df = pd.DataFrame(daily)
    if not df.empty and {"gmv", "orders"} <= set(df.columns):
        gmv = float(df["gmv"].sum())
        orders = int(df["orders"].sum())
        aov = round(gmv / orders, 2) if orders else 0.0
        prev_gmv = float(sum(d["gmv"] for d in sales.get("prev_daily", [])))
        prev_orders = int(sum(d["orders"] for d in sales.get("prev_daily", [])))
        gmv_change = _pct(gmv, prev_gmv)
        order_change = _pct(orders, prev_orders)
        aov_change = _pct(aov, prev_gmv / prev_orders if prev_orders else 0)

        # 趋势：后 3 天 vs 前 3 天均值
        recent = df.tail(3)["gmv"].mean()
        earlier = df.head(3)["gmv"].mean()
        trend = "down" if recent < earlier * 0.97 else ("up" if recent > earlier * 1.03 else "flat")
    else:
        gmv = float(summary.get("gmv", 0.0) or 0.0)
        orders = int(summary.get("order_count", 0) or 0)
        aov = float(summary.get("avg_order_value", 0.0) or 0.0)
        gmv_change = summary.get("gmv_change_pct")
        order_change = summary.get("order_change_pct")
        prev_gmv = float(summary.get("prev_gmv", 0.0) or 0.0)
        prev_orders = int(summary.get("prev_order_count", 0) or 0)
        aov_change = None
        trend = "unknown"

    # 推广侧指标：ROI / 点击转化率
    camp = (campaign_data or {}).get("data", {}) if isinstance(campaign_data, dict) and "data" in campaign_data else (campaign_data or {})
    campaigns = camp.get("campaigns", []) or []
    total_spent = float(camp.get("total_spent", 0.0) or 0.0)
    total_clicks = sum(c.get("clicks", 0) or 0 for c in campaigns)
    total_conv = sum(c.get("conversions", 0) or 0 for c in campaigns)
    campaign_conversion_rate = round(total_conv / total_clicks * 100, 2) if total_clicks else None
    rois = [c["roi"] for c in campaigns if c.get("roi") is not None]
    avg_roi = round(sum(rois) / len(rois), 2) if rois else None

    def _market_payload(value: dict | None) -> dict:
        return (value or {}).get("data", {}) if isinstance(value, dict) and "data" in value else (value or {})

    def _selected_store(payload: dict) -> dict:
        stores = payload.get("stores", []) or []
        return stores[0] if len(stores) == 1 else (payload.get("total", {}) or {})

    traffic_payload = _market_payload(traffic_data)
    transaction_payload = _market_payload(transaction_data)
    consult_payload = _market_payload(consult_data)
    traffic = _selected_store(traffic_payload)
    transaction = _selected_store(transaction_payload)
    consult = _selected_store(consult_payload)

    exposure_users = int(traffic.get("exposure_users", 0) or 0)
    visit_users = int(traffic.get("visit_users", 0) or 0)
    intention_users = int(traffic.get("intention_users", 0) or 0)
    traffic_order_users = int(traffic.get("order_users", 0) or 0)
    online_order_amount = float(transaction.get("order_amount", 0) or 0)
    online_order_coupons = int(transaction.get("order_coupons", 0) or 0)
    verify_amount = float(transaction.get("verify_amount", 0) or 0)
    refund_amount = float(transaction.get("refund_amount", 0) or 0)
    consult_users = int(consult.get("consult_users", 0) or 0)
    consult_leads = int(consult.get("consult_leads", 0) or 0)
    reply5_rate = float(consult.get("reply5_rate", 0) or 0) if consult else None

    metrics = {
        "gmv": round(gmv, 2),
        "order_count": orders,
        "avg_order_value": aov,
        "gmv_change_pct": gmv_change,
        "order_change_pct": order_change,
        "aov_change_pct": aov_change,
        "campaign_conversion_rate": campaign_conversion_rate,
        "campaign_roi_avg": avg_roi,
        "campaign_total_spent": round(total_spent, 2),
        "exposure_users": exposure_users,
        "visit_users": visit_users,
        "exposure_visit_rate": round(visit_users / exposure_users * 100, 2) if exposure_users else None,
        "intention_users": intention_users,
        "visit_intention_rate": round(intention_users / visit_users * 100, 2) if visit_users else None,
        "traffic_order_users": traffic_order_users,
        "online_order_amount": round(online_order_amount, 2),
        "online_order_coupons": online_order_coupons,
        "verify_amount": round(verify_amount, 2),
        "refund_amount": round(refund_amount, 2),
        "refund_rate": round(refund_amount / online_order_amount * 100, 2) if online_order_amount else None,
        "consult_users": consult_users,
        "consult_leads": consult_leads,
        "consult_lead_rate": round(consult_leads / consult_users * 100, 2) if consult_users else None,
        "reply5_rate": reply5_rate,
        "trend": trend,
    }

    # ---------------- 维度分解（品类贡献度） ----------------
    dimension_breakdown: list[dict[str, Any]] = []
    if category:
        cur_total = gmv
        prev_total = prev_gmv if prev_gmv else 0.0
        total_delta = cur_total - prev_total
        for c in category:
            cur = float(c.get("gmv", 0) or 0)
            prev = float(c.get("prev_gmv") or 0)
            dimension_breakdown.append({
                "dimension": "category",
                "name": c.get("category", "未知"),
                "gmv": cur,
                "share": round(cur / cur_total * 100, 2) if cur_total else 0.0,
                "prev_gmv": prev,
                "change_pct": _pct(cur, prev),
                "contribution": round((cur - prev) / total_delta * 100, 2) if total_delta else 0.0,
            })
        dimension_breakdown.sort(key=lambda x: x["contribution"], reverse=True)  # 对整体下滑贡献度从大到小

    # ---------------- 异常检测 ----------------
    anomalies: list[dict[str, str]] = []
    if gmv_change is not None and gmv_change <= -10:
        anomalies.append({
            "level": "high",
            "type": "revenue_drop",
            "message": f"营业额环比下降 {gmv_change}%（阈值 -10%）",
            "evidence": f"本期 {gmv} vs 上期 {prev_gmv}",
        })
    if order_change is not None and order_change <= -10:
        anomalies.append({
            "level": "high",
            "type": "traffic_drop",
            "message": f"订单量环比下降 {order_change}%（客流下滑信号）",
            "evidence": f"本期 {orders} 单 vs 上期 {prev_orders} 单",
        })
    if avg_roi is not None and avg_roi < 1.5:
        anomalies.append({
            "level": "medium",
            "type": "roi_low",
            "message": f"推广平均 ROI={avg_roi} 低于健康线 1.5",
            "evidence": "见 campaigns 明细",
        })
    if camp.get("budget_warn"):
        anomalies.append({
            "level": "medium",
            "type": "budget_overrun",
            "message": "存在预算消耗 >80% 的推广计划（预算花超预警）",
            "evidence": "见 campaigns.spent_ratio",
        })
    if campaign_conversion_rate is not None and campaign_conversion_rate < 3.0:
        anomalies.append({
            "level": "low",
            "type": "conversion_low",
            "message": f"推广点击转化率 {campaign_conversion_rate}% 偏低",
            "evidence": "见 campaigns 明细",
        })
    freshness = {
        name: payload.get("freshness", {})
        for name, payload in (
            ("traffic", traffic_payload),
            ("transaction", transaction_payload),
            ("consult", consult_payload),
        ) if payload
    }
    stale_sets = [name for name, info in freshness.items() if info.get("stale")]
    if stale_sets:
        anomalies.append({
            "level": "high",
            "type": "data_stale",
            "message": f"{','.join(stale_sets)} 数据已超过新鲜度阈值",
            "evidence": "; ".join(
                f"{name}: 截止 {info.get('period_end')}，距今 {info.get('age_days')} 天"
                for name, info in freshness.items() if info.get("stale")
            ),
        })

    # ---------------- 归因因子 ----------------
    factors: list[dict[str, str]] = []
    if dimension_breakdown:
        worst = dimension_breakdown[0]  # 对整体下滑贡献最大的品类（已降序）
        if worst["change_pct"] is not None and worst["change_pct"] <= -20:
            factors.append({
                "type": "category_drop",
                "impact": f"{worst['name']}品类贡献整体下滑 {worst['contribution']}%（品类销售额环比 {worst['change_pct']}%）",
                "evidence": f"{worst['name']} gmv {worst['gmv']} vs 上期 {worst['prev_gmv']}",
                "suggestion": "检查该品类库存/下架/供应链情况，恢复上架并安排恢复性活动",
            })
    if order_change is not None and order_change <= -10:
        factors.append({
            "type": "traffic_drop",
            "impact": f"订单量下滑 {order_change}%",
            "evidence": "订单量同步下滑而客单价变化有限 → 判断为客流驱动",
            "suggestion": "结合渠道投放（见推广数据）与门店现场引流动作",
        })
    # 单条计划预算花超或 ROI 低于健康线（不要求整体均值）
    warn_camps = [
        c for c in campaigns
        if c.get("budget_warn") or (c.get("roi") is not None and float(c["roi"]) < 1.5)
    ]
    if warn_camps:
        c0 = warn_camps[0]
        roi_text = f"ROI={c0.get('roi')}" if c0.get("roi") is not None else "ROI 未知"
        spent_ratio = c0.get("spent_ratio")
        if spent_ratio is not None:
            ratio_text = f"预算消耗 {float(spent_ratio) * 100:.0f}%，"
        else:
            ratio_text = ""
        factors.append({
            "type": "campaign_budget",
            "impact": f"推广计划「{c0.get('name')}」{ratio_text}{roi_text} 低于健康线",
            "evidence": f"spent={c0.get('spent')} conversions={c0.get('conversions')}",
            "suggestion": "核查低效投放的渠道、素材与门店承接情况；必要时向店长同步现场转化与客群反馈，再评估后续策略",
        })
    if visit_users and intention_users / visit_users < 0.05:
        factors.append({
            "type": "visit_conversion_low",
            "impact": f"访问到意向转化率仅 {intention_users / visit_users * 100:.2f}%",
            "evidence": f"访问 {visit_users} 人，意向 {intention_users} 人",
            "suggestion": "检查门店页面套餐、价格信息、评价展示与咨询入口是否清晰",
        })
    if reply5_rate is not None and consult_users and reply5_rate < 80:
        factors.append({
            "type": "slow_consult_reply",
            "impact": f"5分钟内回复率为 {reply5_rate:.2f}%",
            "evidence": f"咨询 {consult_users} 人，留咨 {consult_leads} 人",
            "suggestion": "按咨询高峰时段安排值守并统一首轮回复话术",
        })
    if online_order_amount and refund_amount / online_order_amount > 0.2:
        factors.append({
            "type": "refund_high",
            "impact": f"退款金额占线上下单金额 {refund_amount / online_order_amount * 100:.2f}%",
            "evidence": f"退款 {refund_amount:.2f} 元 / 下单 {online_order_amount:.2f} 元",
            "suggestion": "核查高退款套餐、预约承接和不可用日期说明",
        })
    if not factors:
        factors.append({
            "type": "normal",
            "impact": "未发现显著异常因子",
            "evidence": "各项指标在正常波动区间",
            "suggestion": "维持现有运营策略，持续监控",
        })

    result = {
        "metrics": metrics,
        "dimension_breakdown": dimension_breakdown,
        "anomalies": anomalies,
        "factors": factors,
        "top_products": top_products,
        "data_freshness": freshness,
    }
    return {"success": True, "data": result, "error": None}
