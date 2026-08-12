"""
市场数据工具：客流 / 交易 / 在线咨询 / 门店综合排名
====================================================
读取经营参谋真实数据表（traffic_reports / traffic_leads / transaction_reports /
consult_reports / consult_hourly），提供按门店/时间聚合的查询与综合排名。

- 时间口径：以 data_snapshots 记录的最新 period 为准（近 7 天）
- 返回统一 {"success", "data", "error"}
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from langchain_core.tools import tool
from sqlalchemy import func, select

from config.settings import settings
from database.mysql import get_session_factory
from database.models import (
    ConsultReport,
    DataSnapshot,
    TrafficLead,
    TrafficReport,
    TransactionReport,
)

logger = logging.getLogger(__name__)


def _latest_period(dataset: str) -> tuple[date, date]:
    """取指定数据集最新的 period（默认 08-04~08-10 这类近 7 天）。"""
    try:
        with get_session_factory()() as session:
            snap = session.execute(
                select(DataSnapshot)
                .where(DataSnapshot.dataset == dataset)
                .order_by(DataSnapshot.id.desc())
            ).scalars().first()
        if snap and snap.period_start and snap.period_end:
            return snap.period_start, snap.period_end
    except Exception as exc:
        logger.warning("读取快照失败：%s", exc)
    return date.today() - timedelta(days=7), date.today()


def _latest_period_default() -> tuple[date, date]:
    return _latest_period("traffic")


@tool
def get_traffic_data(
    store_id: int | None = None,
    days: int | None = None,
    rank: bool = False,
) -> dict:
    """查询客流数据（曝光/访问/意向转化等）。

    参数：store_id 点评门店ID（不传返回全部门店汇总）；days 回看天数（默认取快照最近 7 天）；
    rank=True 时按'访问人数'降序返回门店排名。
    """
    try:
        ps, pe = _latest_period("traffic")
        if days:
            pe = min(pe, ps + timedelta(days=days - 1))
        with get_session_factory()() as session:
            stmt = select(
                TrafficReport.store_id,
                TrafficReport.store_name,
                func.sum(TrafficReport.exposure_users).label("exposure_users"),
                func.sum(TrafficReport.exposure_views).label("exposure_views"),
                func.sum(TrafficReport.visit_users).label("visit_users"),
                func.sum(TrafficReport.visit_views).label("visit_views"),
                func.sum(TrafficReport.intention_users).label("intention_users"),
                func.sum(TrafficReport.order_users).label("order_users"),
                func.sum(TrafficReport.lead_users).label("lead_users"),
            ).where(
                TrafficReport.report_date >= ps,
                TrafficReport.report_date <= pe,
            )
            if store_id:
                stmt = stmt.where(TrafficReport.store_id == store_id)
            stmt = stmt.group_by(TrafficReport.store_id, TrafficReport.store_name)
            rows = session.execute(stmt).all()

        stores = [
            {
                "store_id": r.store_id,
                "store_name": r.store_name,
                "exposure_users": int(r.exposure_users or 0),
                "exposure_views": int(r.exposure_views or 0),
                "visit_users": int(r.visit_users or 0),
                "visit_views": int(r.visit_views or 0),
                "intention_users": int(r.intention_users or 0),
                "order_users": int(r.order_users or 0),
                "lead_users": int(r.lead_users or 0),
            }
            for r in rows
        ]
        stores.sort(key=lambda x: x["visit_users"], reverse=True)
        total = {
            "stores": len(stores),
            "exposure_users": sum(s["exposure_users"] for s in stores),
            "visit_users": sum(s["visit_users"] for s in stores),
            "intention_users": sum(s["intention_users"] for s in stores),
        }
        if rank:
            for i, s in enumerate(stores, 1):
                s["rank"] = i
            stores = stores[:20]
        return {"success": True, "data": {
            "period": f"{ps} ~ {pe}", "is_real": True, "total": total, "stores": stores,
        }, "error": None}
    except Exception as exc:
        logger.error("get_traffic_data 失败：%s", exc)
        return {"success": False, "data": {}, "error": str(exc)}


@tool
def get_transaction_data(
    store_id: int | None = None,
    days: int | None = None,
    rank: bool = False,
) -> dict:
    """查询交易数据（下单金额/核销金额/退款等，按门店聚合）。

    参数：store_id 点评门店ID（不传返回全部门店）；days 回看天数；rank=True 按'下单金额'降序排名。
    """
    try:
        ps, pe = _latest_period("transaction")
        if days:
            pe = min(pe, ps + timedelta(days=days - 1))
        with get_session_factory()() as session:
            stmt = select(
                TransactionReport.store_id,
                TransactionReport.store_name,
                func.sum(TransactionReport.order_users).label("order_users"),
                func.sum(TransactionReport.order_coupons).label("order_coupons"),
                func.sum(TransactionReport.order_amount).label("order_amount"),
                func.sum(TransactionReport.verify_users).label("verify_users"),
                func.sum(TransactionReport.verify_coupons).label("verify_coupons"),
                func.sum(TransactionReport.verify_amount).label("verify_amount"),
                func.sum(TransactionReport.refund_coupons).label("refund_coupons"),
                func.sum(TransactionReport.refund_amount).label("refund_amount"),
            ).where(
                TransactionReport.report_date >= ps,
                TransactionReport.report_date <= pe,
            )
            if store_id:
                stmt = stmt.where(TransactionReport.store_id == store_id)
            stmt = stmt.group_by(TransactionReport.store_id, TransactionReport.store_name)
            rows = session.execute(stmt).all()

        stores = [
            {
                "store_id": r.store_id,
                "store_name": r.store_name,
                "order_users": int(r.order_users or 0),
                "order_coupons": int(r.order_coupons or 0),
                "order_amount": round(float(r.order_amount or 0), 2),
                "verify_users": int(r.verify_users or 0),
                "verify_coupons": int(r.verify_coupons or 0),
                "verify_amount": round(float(r.verify_amount or 0), 2),
                "refund_coupons": int(r.refund_coupons or 0),
                "refund_amount": round(float(r.refund_amount or 0), 2),
            }
            for r in rows
        ]
        stores.sort(key=lambda x: x["order_amount"], reverse=True)
        total = {
            "stores": len(stores),
            "order_amount": sum(s["order_amount"] for s in stores),
            "verify_amount": sum(s["verify_amount"] for s in stores),
            "order_coupons": sum(s["order_coupons"] for s in stores),
        }
        if rank:
            for i, s in enumerate(stores, 1):
                s["rank"] = i
            stores = stores[:20]
        return {"success": True, "data": {
            "period": f"{ps} ~ {pe}", "is_real": True, "total": total, "stores": stores,
        }, "error": None}
    except Exception as exc:
        logger.error("get_transaction_data 失败：%s", exc)
        return {"success": False, "data": {}, "error": str(exc)}


@tool
def get_consult_data(
    store_id: int | None = None,
    days: int | None = None,
    rank: bool = False,
) -> dict:
    """查询在线咨询数据（咨询人数/留咨/回复率，按门店聚合）。

    参数：store_id 点评门店ID（不传返回全部门店）；days 回看天数；rank=True 按'咨询人数'降序排名。
    """
    try:
        ps, pe = _latest_period("consult")
        if days:
            pe = min(pe, ps + timedelta(days=days - 1))
        with get_session_factory()() as session:
            stmt = select(
                ConsultReport.store_id,
                ConsultReport.store_name,
                func.sum(ConsultReport.consult_users).label("consult_users"),
                func.sum(ConsultReport.consult_leads).label("consult_leads"),
                func.avg(ConsultReport.reply30_rate).label("reply30_rate"),
                func.avg(ConsultReport.reply5_rate).label("reply5_rate"),
            ).where(
                ConsultReport.report_date >= ps,
                ConsultReport.report_date <= pe,
            )
            if store_id:
                stmt = stmt.where(ConsultReport.store_id == store_id)
            stmt = stmt.group_by(ConsultReport.store_id, ConsultReport.store_name)
            rows = session.execute(stmt).all()

        stores = [
            {
                "store_id": r.store_id,
                "store_name": r.store_name,
                "consult_users": int(r.consult_users or 0),
                "consult_leads": int(r.consult_leads or 0),
                "reply30_rate": round(float(r.reply30_rate or 0), 2),
                "reply5_rate": round(float(r.reply5_rate or 0), 2),
            }
            for r in rows
        ]
        stores.sort(key=lambda x: x["consult_users"], reverse=True)
        total = {
            "stores": len(stores),
            "consult_users": sum(s["consult_users"] for s in stores),
            "consult_leads": sum(s["consult_leads"] for s in stores),
        }
        if rank:
            for i, s in enumerate(stores, 1):
                s["rank"] = i
            stores = stores[:20]
        return {"success": True, "data": {
            "period": f"{ps} ~ {pe}", "is_real": True, "total": total, "stores": stores,
        }, "error": None}
    except Exception as exc:
        logger.error("get_consult_data 失败：%s", exc)
        return {"success": False, "data": {}, "error": str(exc)}


@tool
def get_store_ranking(
    weights: str | None = None,
    top_n: int = 10,
) -> dict:
    """门店综合排名：客流 + 交易 + 在线咨询三维度加权打分。

    参数：weights 自定义权重 JSON（如 '{"traffic":0.4,"transaction":0.4,"consult":0.2}'）；
    top_n 返回前 N 名（默认 10）。
    评分：各维度指标做 min-max 归一化后按权重加权（0~100 分）。
    """
    try:
        w = {"traffic": 0.4, "transaction": 0.4, "consult": 0.2}
        if weights:
            import json
            w.update({k: float(v) for k, v in json.loads(weights).items()})

        t = get_traffic_data.invoke({})["data"]
        tx = get_transaction_data.invoke({})["data"]
        c = get_consult_data.invoke({})["data"]

        # 按门店聚合
        merged: dict[int, dict] = {}
        for src, prefix in ((t.get("stores", []), "traffic"), (tx.get("stores", []), "transaction"), (c.get("stores", []), "consult")):
            for s in src:
                sid = s["store_id"]
                d = merged.setdefault(sid, {"store_id": sid, "store_name": s.get("store_name", "")})
                if prefix == "traffic":
                    d["visit_users"] = s.get("visit_users", 0)
                    d["intention_users"] = s.get("intention_users", 0)
                elif prefix == "transaction":
                    d["order_amount"] = s.get("order_amount", 0)
                    d["verify_amount"] = s.get("verify_amount", 0)
                    d["order_users"] = s.get("order_users", 0)
                else:
                    d["consult_users"] = s.get("consult_users", 0)
                    d["consult_leads"] = s.get("consult_leads", 0)

        def _norm(key: str) -> dict:
            vals = [d.get(key, 0) for d in merged.values()]
            mx = max(vals) if vals else 1
            mn = min(vals) if vals else 0
            rng = (mx - mn) or 1
            return {d["store_id"]: (d.get(key, 0) - mn) / rng * 100 for d in merged.values()}

        n_visit = _norm("visit_users")     # 客流：访问人数
        n_order = _norm("order_amount")    # 交易：下单金额
        n_consult = _norm("consult_users")  # 咨询：咨询人数

        rows = []
        for d in merged.values():
            score = (
                w["traffic"] * n_visit.get(d["store_id"], 0)
                + w["transaction"] * n_order.get(d["store_id"], 0)
                + w["consult"] * n_consult.get(d["store_id"], 0)
            )
            rows.append({
                "store_id": d["store_id"],
                "store_name": d["store_name"],
                "visit_users": d.get("visit_users", 0),
                "order_amount": round(d.get("order_amount", 0), 2),
                "consult_users": d.get("consult_users", 0),
                "score": round(score, 1),
                "score_traffic": round(n_visit.get(d["store_id"], 0), 1),
                "score_transaction": round(n_order.get(d["store_id"], 0), 1),
                "score_consult": round(n_consult.get(d["store_id"], 0), 1),
            })
        rows.sort(key=lambda x: x["score"], reverse=True)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        period = t.get("period", "")
        return {"success": True, "data": {
            "period": period,
            "weights": w,
            "rank": rows[:top_n],
            "total_stores": len(rows),
        }, "error": None}
    except Exception as exc:
        logger.error("get_store_ranking 失败：%s", exc)
        return {"success": False, "data": {}, "error": str(exc)}
