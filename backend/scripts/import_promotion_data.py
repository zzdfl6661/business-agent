"""
导入真实推广数据（智选展位数据报告下载的 xls → 清洗 CSV）到 promotion_reports 表
=================================================================================
数据源：backend/data/scraped/cleaned/2026-08-03_2026-08-09_*.csv（analyze_zxz_report 生成）
- 分推广查看 → dimension='adv'（按推广名称聚合，890 行）
- 分时段查看 → dimension='time'（日粒度，7 行）

幂等：先清空 promotion_reports 再导入。
运行：python -m scripts.import_promotion_data
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sqlalchemy import delete, insert  # noqa: E402

from database.models import Base, PromotionReport  # noqa: E402
from database.mysql import create_db_if_not_exists, get_engine, get_session_factory  # noqa: E402

CLEAN_DIR = Path(__file__).resolve().parent.parent / "data" / "scraped" / "cleaned"
PERIOD_START = date(2026, 8, 3)
PERIOD_END = date(2026, 8, 9)

# CSV 列 → PromotionReport 字段映射
COL_MAP = {
    "花费": "spent",
    "曝光": "impressions",
    "点击": "clicks",
    "千次曝光均价": "cpm",
    "点击率": "ctr",
    "感兴趣": "interested",
    "预约及意向": "reservations",
    "收藏": "favorites",
    "查看团购": "view_group",
    "查看优惠促销": "view_coupon",
    "支付订单量": "orders_paid",
    "团购订单量": "orders_group",
    "间接预约及意向": "ind_reservations",
    "间接支付订单量": "ind_orders_paid",
    "间接团购订单量": "ind_orders_group",
    "间接在线咨询沟通量": "ind_consult",
    "新客感兴趣量": "new_customer",
    "分享": "shares",
}


def _to_decimal(v) -> Decimal:
    try:
        return Decimal(str(float(v)))
    except (TypeError, ValueError):
        return Decimal("0")


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def build_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- 分推广维度 ----
    f_adv = CLEAN_DIR / "2026-08-03_2026-08-09_分推广查看.csv"
    if f_adv.exists():
        df = pd.read_csv(f_adv, encoding="utf-8-sig")
        for _, r in df.iterrows():
            name = str(r["名称"]).strip()
            if name in ("总计", "") or pd.isna(r["名称"]):
                continue
            row = {
                "dimension": "adv",
                "name": name[:60],
                "target": None if pd.isna(r.get("人群定向")) else str(r["人群定向"])[:60],
                "report_date": None,
                "period_start": PERIOD_START,
                "period_end": PERIOD_END,
            }
            for csv_col, field in COL_MAP.items():
                if csv_col in r:
                    row[field] = _to_decimal(r[csv_col]) if field not in (
                        "impressions", "clicks", "interested", "reservations", "favorites",
                        "view_group", "view_coupon", "orders_paid", "orders_group",
                        "ind_reservations", "ind_orders_paid", "ind_orders_group",
                        "ind_consult", "new_customer", "shares",
                    ) else _to_int(r[csv_col])
            rows.append(row)
        print(f"分推广维度: {len(rows)} 行")

    # ---- 分时段维度 ----
    f_time = CLEAN_DIR / "2026-08-03_2026-08-09_分时段查看.csv"
    if f_time.exists():
        df = pd.read_csv(f_time, encoding="utf-8-sig")
        cnt = 0
        for _, r in df.iterrows():
            day = str(r["日期"]).strip()
            if day in ("总计", "") or pd.isna(r["日期"]):
                continue
            # 08-03 → 2026-08-03
            try:
                report_date = datetime.strptime(f"{PERIOD_START.year}-{day}", "%Y-%m-%d").date()
            except ValueError:
                continue
            row = {
                "dimension": "time",
                "name": day,
                "target": None,
                "report_date": report_date,
                "period_start": PERIOD_START,
                "period_end": PERIOD_END,
            }
            for csv_col, field in COL_MAP.items():
                if csv_col in r:
                    row[field] = _to_decimal(r[csv_col]) if field not in (
                        "impressions", "clicks", "interested", "reservations", "favorites",
                        "view_group", "view_coupon", "orders_paid", "orders_group",
                        "ind_reservations", "ind_orders_paid", "ind_orders_group",
                        "ind_consult", "new_customer", "shares",
                    ) else _to_int(r[csv_col])
            rows.append(row)
            cnt += 1
        print(f"分时段维度: {cnt} 行")

    return rows


def main():
    t0 = time.time()
    create_db_if_not_exists()
    engine = get_engine()
    Base.metadata.create_all(engine)
    session_factory = get_session_factory()

    rows = build_rows()
    if not rows:
        print("[!] 未找到清洗 CSV（先运行 analyze_zxz_report 生成）")
        return

    with session_factory() as session:
        session.execute(delete(PromotionReport))
        session.execute(insert(PromotionReport), rows)
        session.commit()
        total = session.query(PromotionReport).count()
    print(f"✅ 导入完成: {total} 行（{time.time() - t0:.1f}s）")


if __name__ == "__main__":
    main()
