"""
Mock 业务数据种子脚本（接入真实门店主数据）
==========================================
运行：cd backend && python -m scripts.seed

门店数据来源（优先级）：
1. BIZ_STORES_JSON 指向的 stores.json（美团后台真实门店列表，含 search_keyword/budget_keyword）
2. backend/data/stores.json（项目内快照）
3. 内置 STORE_PLAN（5 家演示门店，兜底）

生成内容（密室逃脱业态）：
- stores    : 真实门店（当前 37 家，全部 enabled）
- products  : 每家 40~65 个（单人票/双人票/主题场次/团建套餐/会员储值）
- orders    : 最近 90 天，每店每天 20~120 单（约 23 万行，分批写入）
- campaigns : 每家 3~4 条推广计划（美团/点评/抖音/小程序）

内置"下降归因"埋点（保证演示可复现）：
1. 1 号店（stores.json 第一行）最近 7 天订单量较前 7 天下降约 30%（客流掉量）
2. 1 号店最近 7 天「主题场次」品类权重下降约 55%（品类掉量）
3. 1 号店存在一条 running 状态、预算消耗 > 80% 的推广计划（预算花超）

幂等：每次运行先清空四张表再重新灌入。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# 保证以 backend 为根运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faker import Faker  # noqa: E402
from sqlalchemy import delete, insert  # noqa: E402

from config.settings import settings  # noqa: E402
from database.models import Base, Campaign, Order, Product, Store  # noqa: E402
from database.mysql import create_db_if_not_exists, get_engine, get_session_factory  # noqa: E402

fake = Faker("zh_CN")
Faker.seed(42)
random.seed(42)

# ---------------- 门店主数据 ----------------
# 兜底演示门店（stores.json 不可用时的 STORE_PLAN 映射）
STORE_PLAN = [
    {"store_name": "华东旗舰店(上海)", "region": "华东", "city": "上海", "address": "上海市黄浦区南京东路100号", "manager_name": "王芳", "open_date": date(2024, 3, 15), "search_keyword": "旗舰", "budget_keyword": None},
    {"store_name": "华东静安店(上海)", "region": "华东", "city": "上海", "address": "上海市静安区南京西路500号", "manager_name": "李强", "open_date": date(2024, 6, 1), "search_keyword": "静安", "budget_keyword": None},
    {"store_name": "华南天河店(广州)", "region": "华南", "city": "广州", "address": "广州市天河区天河路200号", "manager_name": "陈丽", "open_date": date(2024, 5, 20), "search_keyword": "天河", "budget_keyword": None},
    {"store_name": "华南海珠店(广州)", "region": "华南", "city": "广州", "address": "广州市海珠区江南大道中300号", "manager_name": "赵强", "open_date": date(2024, 9, 1), "search_keyword": "海珠", "budget_keyword": None},
    {"store_name": "华北朝阳店(北京)", "region": "华北", "city": "北京", "address": "北京市朝阳区望京街100号", "manager_name": "孙静", "open_date": date(2024, 11, 11), "search_keyword": "朝阳", "budget_keyword": None},
]

# 城市推断（由门店名关键字 → 城市，仅收录高置信项，其余为 None）
CITY_HINTS = [
    ("长宁", "上海"), ("仲盛", "上海"), ("正大", "上海"), ("来福士", "上海"),
    ("长风大悦城", "上海"), ("五角场", "上海"), ("徐家汇", "上海"), ("静安大悦城", "上海"),
    ("松江", "上海"), ("小南门", "上海"), ("迪美", "上海"), ("太阳宫", "上海"), ("外滩", "上海"),
    ("京西大悦城", "北京"), ("长楹", "北京"), ("房山", "北京"), ("崇文", "北京"),
    ("杭州大悦城", "杭州"), ("西溪", "杭州"), ("滨江", "杭州"), ("湖滨", "杭州"),
    ("东吴", "苏州"), ("吾悦", "常州"), ("青悦城", "台州"), ("金鹰", "南京"),
]


def _infer_city(name: str) -> str | None:
    for keyword, city in CITY_HINTS:
        if keyword in name:
            return city
    return None


def load_stores() -> list[dict]:
    """读取真实门店主数据（stores.json），映射为 Store 行；失败时退回内置 STORE_PLAN。"""
    candidates: list[Path] = []
    if settings.stores_json:
        candidates.append(Path(settings.stores_json))
    candidates.append(Path(__file__).resolve().parent.parent / "data" / "stores.json")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 门店 JSON 解析失败 {path}: {exc}")
            continue
        stores: list[dict] = []
        for i, s in enumerate(data.get("stores", []), start=1):
            name = str(s.get("name", "")).strip()
            enabled = s.get("enabled", True)
            if not name:
                continue
            stores.append({
                "id": i,
                "store_code": f"ST{i:04d}",
                "store_name": name,
                "region": None,
                "city": _infer_city(name),
                "address": None,
                "manager_name": None,
                "open_date": None,
                "search_keyword": s.get("search_keyword"),
                "budget_keyword": s.get("budget_keyword"),
                "status": "active" if enabled else "closed",
            })
        if stores:
            print(f"📋 门店主数据来源：{path}（{len(stores)} 家 enabled 门店）")
            return stores

    print("⚠️ stores.json 不可用，使用内置 5 家演示门店")
    return [
        {"id": i, "store_code": f"ST{i:04d}", **plan, "status": "active"}
        for i, plan in enumerate(STORE_PLAN, start=1)
    ]


# ---------------- 商品（密室逃脱业态） ----------------
CATEGORIES = ["单人票", "双人票", "主题场次", "团建套餐", "会员储值"]
PRICE_RANGE = {
    "单人票": (98, 198),
    "双人票": (168, 328),
    "主题场次": (138, 298),
    "团建套餐": (598, 1980),
    "会员储值": (200, 1000),
}
THEMES = ["沉浸解压", "机械解谜", "真人NPC", "魔法互动", "运动闯关", "恐怖惊悚"]


def _build_products(store_id: int) -> list[dict]:
    rows: list[dict] = []
    pid = 1
    for cat in CATEGORIES:
        lo, hi = PRICE_RANGE[cat]
        count = random.randint(8, 13)
        for i in range(count):
            theme = random.choice(THEMES)
            price = round(random.uniform(lo, hi), 2)
            cost = round(price * random.uniform(0.30, 0.45), 2)  # 场地边际成本较低
            rows.append({
                "id": pid,
                "store_id": store_id,
                "product_code": f"P{store_id:03d}{cat[:1]}{i + 1:03d}",
                "product_name": f"{theme}{cat}",
                "category": cat,
                "price": Decimal(str(price)),
                "cost": Decimal(str(cost)),
                "status": "active",
            })
            pid += 1
    return rows


def _build_campaigns(store_id: int, today: date) -> list[dict]:
    """每家门店 3~4 条推广计划；1 号店含"预算花超 88%"的 running 计划。"""
    types = ["满减", "折扣券", "新客立减"]
    channels = ["美团", "大众点评", "抖音", "小程序"]
    statuses = ["planned", "running", "ended", "paused"]
    rows: list[dict] = []
    cid = 1
    for i in range(random.randint(3, 4)):
        campaign_type = random.choice(types)
        channel = random.choice(channels)
        status = random.choice(statuses)
        budget = round(random.uniform(3000, 30000), 2)
        start = today - timedelta(days=random.randint(5, 45))
        end = start + timedelta(days=random.randint(7, 30))

        # 埋点：1 号店一条 running + 预算花超 88%
        if store_id == DROP_STORE_ID and i == 0:
            status = "running"
            channel = "美团"
            campaign_type = "满减"
            budget = 20000.0
            start = today - timedelta(days=14)
            end = today + timedelta(days=7)

        spent = budget * random.uniform(0.1, 0.95)
        cpc = random.uniform(3.0, 6.0)              # 本地生活 CPC 3~6 元（真实）
        conv_rate = random.uniform(0.01, 0.02)      # 点击转化率 1%~2%（高客单真实水平）
        clicks = int(spent / cpc)
        conversions = int(clicks * conv_rate)
        if store_id == DROP_STORE_ID and i == 0:
            spent = budget * 0.88
            clicks = int(spent / 4.0)
            conversions = int(clicks * 0.008)       # 埋点：该计划转化异常低 → ROI<1.5

        rows.append({
            "id": cid,
            "store_id": store_id,
            "campaign_name": f"{campaign_type}推广-{channel}-{store_id:02d}-{i + 1}",
            "campaign_type": campaign_type,
            "budget": Decimal(str(round(budget, 2))),
            "spent_amount": Decimal(str(round(spent, 2))),
            "clicks": clicks,
            "conversions": conversions,
            "start_date": start,
            "end_date": end,
            "status": status,
            "channel": channel,
        })
        cid += 1
    return rows


def _build_orders(products: list[dict], store_id: int, today: date) -> list[dict]:
    """生成最近 DAYS 天订单；对 1 号店注入下滑埋点（低频高客单业态）。"""
    rows: list[dict] = []
    payment_methods = ["wechat", "alipay", "cash", "card"]
    base_weights = [1.0] * len(products)
    for d in range(DAYS - 1, -1, -1):
        day = today - timedelta(days=d)
        is_drop_window = (store_id == DROP_STORE_ID and d < DROP_RECENT_DAYS)

        daily_count = random.randint(20, 120)
        if is_drop_window:
            daily_count = int(daily_count * ORDER_DROP_RATIO)

        weights = base_weights[:]
        if is_drop_window:
            weights = [
                w * (CATEGORY_DROP_RATIO if p["category"] == "主题场次" else 1.0)
                for p, w in zip(products, weights)
            ]

        for _ in range(daily_count):
            product = random.choices(products, weights=weights, k=1)[0]
            qty = random.choices([1, 2, 4], weights=[0.78, 0.15, 0.07], k=1)[0]
            unit_price = float(product["price"])
            discount = 0.0 if random.random() > 0.15 else round(random.uniform(10, 50), 2)
            total = round(unit_price * qty - discount, 2)
            order_time = datetime.combine(
                day,
                datetime.min.time().replace(
                    hour=random.randint(10, 22), minute=random.randint(0, 59)
                ),
            )
            rows.append({
                "order_no": f"{day:%Y%m%d}{store_id:02d}{len(rows):07d}",
                "store_id": store_id,
                "product_id": product["id"],
                "quantity": qty,
                "unit_price": Decimal(str(round(unit_price, 2))),
                "total_amount": Decimal(str(max(total, 0.01))),
                "discount_amount": Decimal(str(round(discount, 2))),
                "payment_method": random.choice(payment_methods),
                "order_time": order_time,
                "order_status": "completed" if random.random() > 0.02 else "refunded",
            })
    return rows


DAYS = 90                # 回看窗口
BATCH = 5000             # 批量写入行数
DROP_STORE_ID = 1        # 埋点门店（stores.json 第一行）
DROP_RECENT_DAYS = 7     # 埋点窗口（最近 N 天）
ORDER_DROP_RATIO = 0.70  # 订单量下滑系数
CATEGORY_DROP_RATIO = 0.45  # 主题场次品类权重系数


def seed(verbose: bool = True) -> None:
    t0 = time.time()
    create_db_if_not_exists()
    engine = get_engine()
    Base.metadata.create_all(engine)  # 建表（幂等，仅建缺失表）
    session_factory = get_session_factory()

    with session_factory() as session:
        # 1. 清空（幂等）
        session.execute(delete(Order))
        session.execute(delete(Campaign))
        session.execute(delete(Product))
        session.execute(delete(Store))
        session.commit()
        if verbose:
            print("[1/4] 已清空旧数据")

        # 2. 门店 + 商品 + 推广计划
        today = date.today()
        store_rows = load_stores()
        product_rows: list[dict] = []
        campaign_rows: list[dict] = []
        product_offset = 0
        campaign_offset = 0
        for s in store_rows:
            prods = _build_products(s["id"])
            for p in prods:
                p["id"] += product_offset
                product_rows.append(p)
            camps = _build_campaigns(s["id"], today)
            for c in camps:
                c["id"] += campaign_offset
                campaign_rows.append(c)
            product_offset += len(prods)
            campaign_offset += len(camps)

        session.execute(insert(Store), store_rows)
        session.execute(insert(Product), product_rows)
        session.execute(insert(Campaign), campaign_rows)
        session.commit()
        if verbose:
            print(f"[2/4] 门店 {len(store_rows)} 家 / 商品 {len(product_rows)} 个 / 推广计划 {len(campaign_rows)} 条")

        # 3. 订单（分批写入）
        total_orders = 0
        for s in store_rows:
            products = [p for p in product_rows if p["store_id"] == s["id"]]
            orders = _build_orders(products, s["id"], today)
            for start in range(0, len(orders), BATCH):
                session.execute(insert(Order), orders[start:start + BATCH])
                session.commit()
            total_orders += len(orders)
            if verbose:
                print(f"     {s['store_code']} {s['store_name'][:20]}… 订单 {len(orders)} 行")

        # 4. 汇总
        counts = {
            "stores": len(store_rows),
            "products": len(product_rows),
            "campaigns": len(campaign_rows),
            "orders": total_orders,
        }
        if verbose:
            print(f"[3/4] 订单合计 {total_orders} 行")
            print(f"[4/4] 种子数据完成 ✅ 耗时 {time.time() - t0:.1f}s → {counts}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="灌入业务数据（真实门店 + 模拟订单/推广）")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()
    seed(verbose=not args.quiet)
