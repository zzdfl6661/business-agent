"""
数据查询工具：get_sales_data / get_campaign_data
================================================
- SQLAlchemy 查询 MySQL（日粒度聚合 + 品类/商品维度）
- 返回值带 previous 窗口，供分析工具计算环比
- 仅真实数据链路：数据库不可用/无数据时返回明确错误，不做任何模拟填充
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from langchain_core.tools import tool
from sqlalchemy import func, select

from database.models import Campaign, Order, Product, PromotionReport
from database.mysql import get_session_factory
from tools.data_cache import get as cache_get
from tools.data_cache import set as cache_set

logger = logging.getLogger(__name__)

PAYMENT_LABEL = {"wechat": "微信", "alipay": "支付宝", "cash": "现金", "card": "银行卡"}


# ---------------------------------------------------------------- 内部工具
def _resolve_range(start_date: str | None, end_date: str | None, days: int) -> tuple[date, date]:
    today = date.today()
    if start_date and end_date:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        days = (end - start).days + 1
    else:
        end = today
        start = today - timedelta(days=days - 1)
    return start, end


def _daily_dates(start: date, end: date) -> list[dict]:
    out, cur = [], start
    while cur <= end:
        out.append({"date": cur.isoformat(), "orders": 0, "gmv": 0.0})
        cur += timedelta(days=1)
    return out


def _query_daily(store_id: int, start: date, end: date) -> list[dict]:
    """MySQL 日粒度聚合（completed 订单）。库不可用时返回空列表。"""
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(
                    func.date(Order.order_time).label("d"),
                    func.count().label("cnt"),
                    func.sum(Order.total_amount).label("amt"),
                )
                .where(
                    Order.store_id == store_id,
                    Order.order_time >= datetime.combine(start, datetime.min.time()),
                    Order.order_time < datetime.combine(end + timedelta(days=1), datetime.min.time()),
                    Order.order_status == "completed",
                )
                .group_by(func.date(Order.order_time))
            ).all()
    except Exception as exc:  # 数据库未就绪 → 上层降级
        logger.warning("sales 查询失败（数据库未就绪？）：%s", exc)
        return []

    by_date = {r.d: r for r in rows}
    result = _daily_dates(start, end)
    for item in result:
        r = by_date.get(date.fromisoformat(item["date"]))
        if r:
            item["orders"] = int(r.cnt)
            item["gmv"] = round(float(r.amt), 2)
    return result


def _query_top_products(store_id: int, start: date, end: date) -> list[dict]:
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                select(
                    Product.product_name.label("name"),
                    Product.category.label("category"),
                    func.count().label("orders"),
                    func.sum(Order.total_amount).label("gmv"),
                )
                .join(Order, Order.product_id == Product.id)
                .where(
                    Order.store_id == store_id,
                    Order.order_time >= datetime.combine(start, datetime.min.time()),
                    Order.order_time < datetime.combine(end + timedelta(days=1), datetime.min.time()),
                    Order.order_status == "completed",
                )
                .group_by(Product.id)
                .order_by(func.sum(Order.total_amount).desc())
                .limit(5)
            ).all()
        return [
            {"name": r.name, "category": r.category, "orders": int(r.orders), "gmv": round(float(r.gmv), 2)}
            for r in rows
        ]
    except Exception as exc:
        logger.warning("top_products 查询失败：%s", exc)
        return []


def _query_category(store_id: int, start: date, end: date) -> list[dict]:
    """品类维度聚合（当前窗口 + 上一窗口），供归因计算贡献度。"""
    def _agg(s: date, e: date) -> dict[str, dict]:
        try:
            with get_session_factory()() as session:
                rows = session.execute(
                    select(
                        Product.category.label("category"),
                        func.count().label("orders"),
                        func.sum(Order.total_amount).label("gmv"),
                    )
                    .join(Order, Order.product_id == Product.id)
                    .where(
                        Order.store_id == store_id,
                        Order.order_time >= datetime.combine(s, datetime.min.time()),
                        Order.order_time < datetime.combine(e + timedelta(days=1), datetime.min.time()),
                        Order.order_status == "completed",
                    )
                    .group_by(Product.category)
                ).all()
            return {r.category: {"orders": int(r.orders), "gmv": round(float(r.gmv), 2)} for r in rows}
        except Exception as exc:
            logger.warning("category 查询失败：%s", exc)
            return {}

    prev_start, prev_end = start - timedelta(days=(end - start).days + 1), start - timedelta(days=1)
    cur_map = _agg(start, end)
    prev_map = _agg(prev_start, prev_end)
    return [
        {
            "category": cat,
            "orders": v["orders"],
            "gmv": v["gmv"],
            "prev_gmv": prev_map.get(cat, {}).get("gmv", 0.0),
        }
        for cat, v in cur_map.items()
    ]


# ---------------------------------------------------------------- 公开工具
@tool
def get_sales_data(
    store_id: int | None = None,
    days: int = 7,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """查询门店销售数据：营业额、订单数、客单价及环比。

    参数：
        store_id 门店ID（缺省返回全部门店汇总）；
        days 回看天数（默认 7）；
        start_date/end_date 显式日期范围（YYYY-MM-DD），优先于 days。

    返回：daily(日粒度当前窗口) + prev_daily(同长度前一窗口) + summary(合计/环比)
          + top_products + category_breakdown。
    """
    params = {"store_id": store_id, "days": days, "start_date": start_date, "end_date": end_date}
    cached = cache_get("get_sales_data", params)
    if cached is not None:
        return cached

    start, end = _resolve_range(start_date, end_date, max(days, 1))
    prev_start, prev_end = start - timedelta(days=(end - start).days + 1), start - timedelta(days=1)

    daily = _query_daily(store_id or 1, start, end)
    prev_daily = _query_daily(store_id or 1, prev_start, prev_end)
    top_products = _query_top_products(store_id or 1, start, end)
    category_breakdown = _query_category(store_id or 1, start, end)
    if not daily:
        return {"success": False, "data": {}, "error": "未查询到数据：请确认数据库已初始化并运行种子脚本/导入真实订单"}

    cur_gmv = sum(d["gmv"] for d in daily)
    cur_orders = sum(d["orders"] for d in daily)
    prev_gmv = sum(d["gmv"] for d in prev_daily)
    prev_orders = sum(d["orders"] for d in prev_daily)

    summary = {
        "store_id": store_id or 1,
        "period": f"{start.isoformat()} ~ {end.isoformat()}",
        "gmv": round(cur_gmv, 2),
        "order_count": cur_orders,
        "avg_order_value": round(cur_gmv / cur_orders, 2) if cur_orders else 0,
        "gmv_change_pct": round((cur_gmv - prev_gmv) / prev_gmv * 100, 2) if prev_gmv else None,
        "order_change_pct": round((cur_orders - prev_orders) / prev_orders * 100, 2) if prev_orders else None,
        "prev_gmv": round(prev_gmv, 2),
        "prev_order_count": prev_orders,
        "data_source": "MySQL",
    }
    result = {
        "success": True,
        "data": {
            "summary": summary,
            "daily": daily,
            "prev_daily": prev_daily,
            "top_products": top_products,
            "category_breakdown": category_breakdown,
        },
        "error": None,
    }
    cache_set("get_sales_data", params, result)  # #7 TTL 缓存（refresh 后失效）
    return result


def _query_real_campaigns(days: int = 30) -> dict:
    """
    读取真实推广数据（promotion_reports，智选展位下载）。
    按推广名称聚合 adv 维度；返回 {found, campaigns, total_spent, aov, period}。
    ROI = (支付订单+团购订单) × 门店客单价 / 花费。
    """
    try:
        with get_session_factory()() as session:
            exists = session.execute(
                select(func.count()).select_from(PromotionReport)
            ).scalar()
            if not exists:
                return {"found": False, "campaigns": [], "total_spent": 0.0, "aov": 0.0, "period": None}

            rows = session.execute(
                select(
                    PromotionReport.name.label("name"),
                    func.sum(PromotionReport.spent).label("spent"),
                    func.sum(PromotionReport.impressions).label("impressions"),
                    func.sum(PromotionReport.clicks).label("clicks"),
                    func.sum(PromotionReport.orders_paid).label("orders_paid"),
                    func.sum(PromotionReport.orders_group).label("orders_group"),
                    func.sum(PromotionReport.interested).label("interested"),
                    func.min(PromotionReport.period_start).label("p_start"),
                    func.max(PromotionReport.period_end).label("p_end"),
                )
                .where(PromotionReport.dimension == "adv")
                .group_by(PromotionReport.name)
                .order_by(func.sum(PromotionReport.spent).desc())
            ).all()

            # 门店平均客单价（近 days 天，全店口径）
            aov_row = session.execute(
                select(func.sum(Order.total_amount) / func.count())
                .where(
                    Order.order_time >= datetime.combine(date.today() - timedelta(days=days), datetime.min.time()),
                    Order.order_status == "completed",
                )
            ).scalar()
            aov = float(aov_row or 0)

        campaigns = []
        for r in rows:
            spent = float(r.spent or 0)
            conversions = int(r.orders_paid or 0) + int(r.orders_group or 0)
            roi = round(conversions * aov / spent, 2) if spent > 0 else None
            campaigns.append({
                "id": len(campaigns) + 1,
                "name": r.name,
                "type": "智选展位",
                "channel": "美团",
                "status": "running",
                "budget": None,
                "spent": round(spent, 2),
                "clicks": int(r.clicks or 0),
                "impressions": int(r.impressions or 0),
                "conversions": conversions,
                "roi": roi,
                "spent_ratio": None,
                "budget_warn": False,
                "period": f"{r.p_start} ~ {r.p_end}" if r.p_start else None,
            })
        period = campaigns[0].get("period") if campaigns else None
        return {
            "found": True,
            "campaigns": campaigns,
            "total_spent": round(sum(c["spent"] for c in campaigns), 2),
            "aov": round(aov, 2),
            "period": period,
        }
    except Exception as exc:
        logger.warning("真实推广数据读取失败：%s", exc)
        return {"found": False, "campaigns": [], "total_spent": 0.0, "aov": 0.0, "period": None}


def _query_campaign_data(
    store_id: int | None = None,
    status: str | None = None,
    days: int = 30,
) -> dict:
    """读取推广数据（真实逻辑，供 get_campaign_data 缓存包装调用）。"""
    # 优先真实推广报告（promotion_reports）
    real = _query_real_campaigns(days=days)
    if real["found"]:
        data = {
            "store_id": store_id or 1,
            "campaigns": real["campaigns"],
            "total_spent": real["total_spent"],
            "budget_warn": False,
            "period": real["period"],
            "aov": real["aov"],
            "note": "数据来源：美团经营宝智选展位数据报告（真实下载数据）",
        }
        return {"success": True, "data": data, "error": None}

    try:
        with get_session_factory()() as session:
            stmt = select(Campaign)
            if store_id:
                stmt = stmt.where(Campaign.store_id == store_id)
            if status:
                stmt = stmt.where(Campaign.status == status)
            # 平均客单价（近 days 天）
            aov_row = session.execute(
                select(func.sum(Order.total_amount) / func.count())
                .where(
                    Order.store_id == (store_id or 1),
                    Order.order_time >= datetime.combine(date.today() - timedelta(days=days), datetime.min.time()),
                    Order.order_status == "completed",
                )
            ).scalar()
            aov = float(aov_row or 0)
            campaigns = []
            for c in session.execute(stmt).scalars():
                spent = float(c.spent_amount or 0)
                roi = round((c.conversions * aov) / spent, 2) if spent > 0 else None
                campaigns.append({
                    "id": c.id, "name": c.campaign_name, "type": c.campaign_type, "channel": c.channel,
                    "status": c.status, "budget": float(c.budget), "spent": round(spent, 2),
                    "clicks": c.clicks or 0, "conversions": c.conversions or 0, "roi": roi,
                    "spent_ratio": round(spent / float(c.budget), 2) if c.budget else 0.0,
                    "budget_warn": float(c.budget) > 0 and spent / float(c.budget) > 0.8,
                })
    except Exception as exc:
        logger.warning("campaign 查询失败：%s", exc)
        return {"success": False, "data": {}, "error": f"推广数据查询失败：{exc}"}

    data = {
        "store_id": store_id or 1,
        "campaigns": campaigns,
        "total_spent": round(sum(c["spent"] for c in campaigns), 2),
        "budget_warn": any(c["budget_warn"] for c in campaigns),
    }
    return {"success": True, "data": data, "error": None}


@tool
def get_campaign_data(
    store_id: int | None = None,
    status: str | None = None,
    days: int = 30,
) -> dict:
    """查询推广计划数据：消耗、点击、转化、ROI（=转化数×客单价/消耗）。

    参数：store_id 门店ID；status 状态过滤(running/planned/ended/paused)；days 回看天数。
    返回：campaigns 列表 + total_spent + budget_warn（是否存在预算消耗>80%）。

    数据源：优先 promotion_reports 真实推广报告（智选展位下载），
    为空时回退 campaigns 表。
    """
    params = {"store_id": store_id, "status": status, "days": days}
    cached = cache_get("get_campaign_data", params)
    if cached is not None:
        return cached
    result = _query_campaign_data(store_id, status, days)
    cache_set("get_campaign_data", params, result)  # #7 TTL 缓存（refresh/确认执行后失效）
    return result
