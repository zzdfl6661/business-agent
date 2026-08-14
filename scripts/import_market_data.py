"""
经营参谋真实数据统一入库
========================
读取 backend/data/scraped/ 下已下载的真实数据（智选展位/客流/交易/在线咨询），
清洗后写入 MySQL，并在 data_snapshots 记录每个数据集的时间范围（近 7 天区间/对比区间）。

时间标识约定（重要）：
- 智选展位报告：period 2026-08-03 ~ 2026-08-09（对比 07-27 ~ 08-02）
- 客流/交易/在线咨询：period 2026-08-04 ~ 2026-08-10（近 7 天）
- 所有明细表保留 report_date（单日粒度）+ period_start/period_end（7 天区间）

幂等：导入前删除对应 dataset 的旧数据与快照，可重复执行。
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from decimal import Decimal

import pandas as pd
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.models import (  # noqa: E402
    ConsultHourly,
    ConsultReport,
    DataSnapshot,
    PromotionReport,
    TrafficLead,
    TrafficReport,
    TransactionReport,
)
from database.mysql import create_db_if_not_exists, get_engine, get_session_factory  # noqa: E402

SC = Path(__file__).resolve().parent.parent / "data" / "scraped"
YEAR = 2026

# ============================ 清洗工具 ============================

def to_float(v) -> float | None:
    """千分位 '10,952.77' / 百分比 '2.91%' / '1598.6秒' → float；空值 → None"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if pd.isna(v):
            return None
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("秒", "").replace("元", "")
    if s in ("", "/", "-", "nan", "None", "—"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def to_int(v) -> int:
    f = to_float(v)
    return int(f) if f is not None else 0


def to_dec(v, digits: int = 2) -> Decimal | None:
    f = to_float(v)
    return Decimal(str(round(f, digits))) if f is not None else None


def to_dec3(v) -> Decimal | None:
    """百分比字段（保留 3 位小数，如 2.910 表示 2.91%）"""
    f = to_float(v)
    return Decimal(str(round(f, 3))) if f is not None else None


def parse_date(v) -> date | None:
    """'2026-08-04' / '08-09' / datetime → date"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    s = str(v).strip()
    if len(s) == 5 and s[2] == "-":          # 08-09 → 2026-08-09
        return date(YEAR, int(s[:2]), int(s[3:]))
    try:
        return pd.to_datetime(s).date()
    except (TypeError, ValueError):
        return None


# ============================ 数据集定义 ============================

def _latest_file(pattern: str) -> Path | None:
    """scraped 目录下匹配 pattern 的最新文件（按修改时间）。"""
    hits = sorted(SC.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _parse_period(fname: str) -> tuple[date, date, date | None, date | None]:
    """从文件名 '2026-08-04_2026-08-10_xxx.xls' 推断 period 与对比区间。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", fname)
    if m:
        start = date.fromisoformat(m.group(1))
        end = date.fromisoformat(m.group(2))
        return start, end, start - timedelta(days=7), start - timedelta(days=1)
    return date(2026, 8, 3), date(2026, 8, 9), date(2026, 7, 27), date(2026, 8, 2)


def load_promotion():
    """智选展位 4 维度 → promotion_reports（dimension: time/adv/aud/cre）

    文件动态匹配最新（download 脚本每次下载新时间段，避免写死日期）。
    """
    dim_files = {
        "time": _latest_file("*_分时段查看.xls"),
        "adv": _latest_file("*_分推广查看.xls"),
        "aud": _latest_file("*_分人群查看.xls"),
        "cre": _latest_file("*_分创意查看.xls"),
    }
    # 时间段：取任意一个最新文件名推断（4 维度同时间段）
    fname0 = str(next((f.name for f in dim_files.values() if f), "2026-08-03_2026-08-09_分时段查看.xls"))
    period_start, period_end, compare_start, compare_end = _parse_period(fname0)
    rows = []
    dims = [
        ("time", dim_files["time"], None, None),   # (dim, 文件, name列, target列)
        ("adv",  dim_files["adv"], "名称", "人群定向"),
        ("aud",  dim_files["aud"], "推广名称", "人群包/标签"),
        ("cre",  dim_files["cre"], "名称", None),
    ]
    for dim, fp, name_col, target_col in dims:
        if fp is None:
            print(f"  ⚠️ 跳过缺失维度文件: {dim}")
            continue
        df = pd.read_excel(fp)
        for _, r in df.iterrows():
            if dim == "time":
                name = str(r.get("日期", "")).strip()
                if name == "总计":
                    continue
                target, rdate = None, parse_date(r.get("日期"))
            else:
                name = str(r.get(name_col, "")).strip()
                if name in ("总计", "nan", "None", ""):
                    continue
                target = str(r.get(target_col, "")).strip() if target_col else None
                if target in ("nan", "None", "/", ""):
                    target = None
                rdate = None
            rows.append({
                "dimension": dim, "name": name[:64], "target": target,
                "report_date": rdate,
                "spent": to_dec(r.get("花费")), "impressions": to_int(r.get("曝光")),
                "clicks": to_int(r.get("点击")), "cpm": to_dec(r.get("千次曝光均价")),
                "ctr": to_dec3(r.get("点击率")), "interested": to_int(r.get("感兴趣")),
                "reservations": to_int(r.get("预约及意向")), "favorites": to_int(r.get("收藏")),
                "view_group": to_int(r.get("查看团购")), "view_coupon": to_int(r.get("查看优惠促销")),
                "orders_paid": to_int(r.get("支付订单量")), "orders_group": to_int(r.get("团购订单量")),
                "ind_reservations": to_int(r.get("间接预约及意向")),
                "ind_orders_paid": to_int(r.get("间接支付订单量")),
                "ind_orders_group": to_int(r.get("间接团购订单量")),
                "ind_consult": to_int(r.get("间接在线咨询沟通量")),
                "new_customer": to_int(r.get("新客感兴趣量")), "shares": to_int(r.get("分享数")),
                "period_start": period_start, "period_end": period_end,
            })
    return rows, period_start, period_end, compare_start, compare_end


def load_traffic():
    """客流分析：变化趋势(traffic_reports) + 引流用户(traffic_leads)"""
    period_start, period_end = date(2026, 8, 4), date(2026, 8, 10)
    traffic, leads = [], []
    fp1 = next(SC.glob("客流分析_1_客流数据*.xlsx"), None)
    if fp1:
        df = pd.read_excel(fp1)
        for _, r in df.iterrows():
            traffic.append({
                "report_date": parse_date(r.get("日期")),
                "store_id": to_int(r.get("门店ID")), "store_name": str(r.get("门店名称"))[:128],
                "province": str(r.get("省份")) if pd.notna(r.get("省份")) else None,
                "city": str(r.get("城市")) if pd.notna(r.get("城市")) else None,
                "exposure_users": to_int(r.get("曝光人数")), "exposure_views": to_int(r.get("曝光次数")),
                "visit_users": to_int(r.get("访问人数")), "visit_views": to_int(r.get("访问次数")),
                "exp_visit_rate": to_dec3(r.get("曝光访问转化率")),
                "intention_users": to_int(r.get("意向转化人数")), "intention_rate": to_dec3(r.get("意向转化率")),
                "order_users": to_int(r.get("下单人数")), "lead_users": to_int(r.get("留资人数")),
                "collect_total": to_int(r.get("累计收藏人数")), "collect_new": to_int(r.get("新增收藏人数")),
                "checkin_new": to_int(r.get("新增打卡人数")),
                "period_start": period_start, "period_end": period_end,
            })
    fp3 = next(SC.glob("客流分析_3_引流用户数据*.xlsx"), None)
    if fp3:
        df = pd.read_excel(fp3)
        for _, r in df.iterrows():
            leads.append({
                "report_date": parse_date(r.get("日期")),
                "store_id": to_int(r.get("点评门店id")), "store_name": str(r.get("门店名称"))[:128],
                "platform": str(r.get("平台")) if pd.notna(r.get("平台")) else None,
                "meituan_leads": to_int(r.get("美团引流顾客")),
                "no_preaction": to_int(r.get("没有提前电话、咨询、预约、留资或购买团购")),
                "pre_contact": to_int(r.get("提前电话/咨询/预约/留资了")),
                "pre_purchase": to_int(r.get("提前购买了团购")),
                "natural_customers": to_int(r.get("自然到店顾客")),
                "purchase_after_arrival": to_int(r.get("到店后购买了团购")),
                "browse_after_arrival": to_int(r.get("没买团购，但到店后线上浏览了信息")),
                "potential_customers": to_int(r.get("潜在顾客")),
                "seen_no_action": to_int(r.get("看过门店，但没有进一步动作，也没去其他门店")),
                "went_other": to_int(r.get("去了其他门店")),
                "period_start": period_start, "period_end": period_end,
            })
    return traffic, leads, period_start, period_end


def load_transaction():
    """交易分析-商品明细（1434 行明细版）"""
    period_start, period_end = date(2026, 8, 4), date(2026, 8, 10)
    rows = []
    fp = sorted(SC.glob("商品交易数据-2026*.xlsx"), key=lambda p: p.stat().st_size, reverse=True)[0]
    df = pd.read_excel(fp)
    for _, r in df.iterrows():
        rows.append({
            "report_date": parse_date(r.get("日期")),
            "product_type": str(r.get("商品类型")) if pd.notna(r.get("商品类型")) else None,
            "product_id": to_int(r.get("商品ID")), "product_name": str(r.get("商品名称"))[:128],
            "province": str(r.get("省份")) if pd.notna(r.get("省份")) else None,
            "city": str(r.get("城市")) if pd.notna(r.get("城市")) else None,
            "store_id": to_int(r.get("点评门店ID")), "store_name": str(r.get("门店名称"))[:128],
            "order_users": to_int(r.get("下单人数")), "order_coupons": to_int(r.get("下单券数")),
            "order_amount_orig": to_dec(r.get("下单金额（原价）")), "order_amount": to_dec(r.get("下单金额")),
            "verify_users": to_int(r.get("核销人数")), "verify_coupons": to_int(r.get("核销券数")),
            "verify_amount_orig": to_dec(r.get("核销金额（原价）")), "verify_amount": to_dec(r.get("核销金额")),
            "refund_coupons": to_int(r.get("退款券数")), "refund_amount": to_dec(r.get("退款金额（原价）")),
            "period_start": period_start, "period_end": period_end,
        })
    return rows, period_start, period_end


def load_consult():
    """在线咨询：总览(consult_reports) + 分时段(consult_hourly，已补门店名)"""
    period_start, period_end = date(2026, 8, 4), date(2026, 8, 10)
    reports, hourly = [], []
    fp_z = next(SC.glob("在线咨询数据-2026*.xlsx"), None)
    if fp_z:
        df = pd.read_excel(fp_z)
        for _, r in df.iterrows():
            reports.append({
                "report_date": parse_date(r.get("日期")),
                "store_id": to_int(r.get("点评门店id")), "store_name": str(r.get("门店名称"))[:128],
                "consult_users": to_int(r.get("在线咨询人数")), "consult_leads": to_int(r.get("在线咨询留咨数")),
                "lead_rate": to_dec3(r.get("咨询留资转化率")),
                "avg_response_sec": to_dec(r.get("平均响应时长")),
                "reply5_rate": to_dec3(r.get("5分钟内回复率")), "reply30_rate": to_dec3(r.get("30秒内回复率")),
                "period_start": period_start, "period_end": period_end,
            })
    fp_t = SC / "在线咨询_分时段_含门店名.csv"
    if fp_t.exists():
        df = pd.read_csv(fp_t, encoding="utf-8-sig")
        for _, r in df.iterrows():
            hourly.append({
                "report_date": parse_date(r.get("日期")),
                "store_id": to_int(r.get("点评门店id")), "store_name": str(r.get("门店名称"))[:128],
                "hour": to_int(r.get("时间")), "consult_users": to_int(r.get("咨询人数")),
                "reply30_rate": to_dec3(r.get("30秒内回复率")), "reply5_rate": to_dec3(r.get("5分钟内回复率")),
                "avg_response_sec": to_dec(r.get("平均响应时长（秒）")),
                "period_start": period_start, "period_end": period_end,
            })
    return reports, hourly, period_start, period_end


# ============================ 入库 ============================

def main():
    create_db_if_not_exists()
    engine = get_engine()
    # 建表（新表：data_snapshots/traffic_reports/traffic_leads/transaction_reports/consult_reports/consult_hourly）
    from database.models import Base
    Base.metadata.create_all(engine)

    sf = get_session_factory()
    with sf() as session:
        print("=" * 60)
        print("【1/4】智选展位 4 维度 → promotion_reports")
        promo, ps, pe, cs, ce = load_promotion()
        session.execute(delete(PromotionReport))
        session.execute(delete(DataSnapshot).where(DataSnapshot.dataset == "campaign"))  # 幂等：删旧快照
        session.bulk_insert_mappings(PromotionReport, promo)
        session.add(DataSnapshot(dataset="campaign", period_start=ps, period_end=pe,
                                 compare_start=cs, compare_end=ce, rows=len(promo),
                                 source_files="智选展位4维度xls", note="智选展位数据报告(4维度)"))
        print(f"  ✅ 写入 {len(promo)} 行（time/adv/aud/cre），period {ps}~{pe}，对比 {cs}~{ce}")

        print("【2/4】客流分析 → traffic_reports + traffic_leads")
        traffic, leads, ps2, pe2 = load_traffic()
        session.execute(delete(TrafficReport))
        session.execute(delete(TrafficLead))
        session.execute(delete(DataSnapshot).where(DataSnapshot.dataset == "traffic"))
        session.bulk_insert_mappings(TrafficReport, traffic)
        session.bulk_insert_mappings(TrafficLead, leads)
        session.add(DataSnapshot(dataset="traffic", period_start=ps2, period_end=pe2,
                                 rows=len(traffic) + len(leads),
                                 source_files="客流数据/引流用户数据xlsx", note="客流分析-变化趋势+引流用户"))
        print(f"  ✅ 客流变化 {len(traffic)} 行 + 引流 {len(leads)} 行，period {ps2}~{pe2}")

        print("【3/4】交易分析 → transaction_reports")
        trans, ps3, pe3 = load_transaction()
        session.execute(delete(TransactionReport))
        session.execute(delete(DataSnapshot).where(DataSnapshot.dataset == "transaction"))
        session.bulk_insert_mappings(TransactionReport, trans)
        session.add(DataSnapshot(dataset="transaction", period_start=ps3, period_end=pe3,
                                 rows=len(trans), source_files="商品交易数据(明细)xlsx",
                                 note="交易分析-商品明细(1434行)"))
        print(f"  ✅ 写入 {len(trans)} 行，period {ps3}~{pe3}")

        print("【4/4】在线咨询 → consult_reports + consult_hourly")
        cons, hourly, ps4, pe4 = load_consult()
        session.execute(delete(ConsultReport))
        session.execute(delete(ConsultHourly))
        session.execute(delete(DataSnapshot).where(DataSnapshot.dataset == "consult"))
        session.bulk_insert_mappings(ConsultReport, cons)
        session.bulk_insert_mappings(ConsultHourly, hourly)
        session.add(DataSnapshot(dataset="consult", period_start=ps4, period_end=pe4,
                                 rows=len(cons) + len(hourly),
                                 source_files="在线咨询数据/分时段咨询数据xlsx",
                                 note="在线咨询总览+分时段(含门店名)"))
        print(f"  ✅ 总览 {len(cons)} 行 + 分时段 {len(hourly)} 行，period {ps4}~{pe4}")

        session.commit()
        print("\n✅ 全部入库完成")


if __name__ == "__main__":
    main()
