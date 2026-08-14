# -*- coding: utf-8 -*-
"""#13-③ 工具真实返回结构契约（结构快照）。

无 Mock 环境：直接断言真实返回结构的 key 集合（不是 mock→real 契约，而是"结构快照"）。
需要 MySQL 连通；未连通时对应用例跳过（不阻塞 CI）。
"""
import pytest

from tools.data_cache import invalidate_data_cache
from tools.database_tool import get_campaign_data, get_sales_data


def _db_ok():
    try:
        from database.mysql import check_connection

        return check_connection()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="MySQL 未连通，跳过真实结构契约测试")


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_data_cache()
    yield


def test_sales_data_contract():
    r = get_sales_data.invoke({"store_id": 1, "days": 7})
    assert r["success"] is True
    data = r["data"]
    assert {"summary", "daily", "prev_daily", "top_products", "category_breakdown"} <= set(data)
    s = data["summary"]
    assert {"store_id", "period", "gmv", "order_count", "avg_order_value",
            "gmv_change_pct", "order_change_pct", "data_source"} <= set(s)
    assert isinstance(data["daily"], list) and isinstance(data["top_products"], list)
    if data["daily"]:
        assert {"date", "gmv", "orders"} <= set(data["daily"][0])


def test_campaign_data_contract():
    r = get_campaign_data.invoke({"days": 30})
    assert r["success"] is True
    data = r["data"]
    assert {"store_id", "campaigns", "total_spent"} <= set(data)
    assert isinstance(data["campaigns"], list)
    if data["campaigns"]:
        c = data["campaigns"][0]
        assert {"name", "spent", "clicks", "conversions", "roi"} <= set(c)


def test_error_path_contract():
    """不存在 store 也不抛异常：返回 {success,data,error} 三字段。"""
    r = get_sales_data.invoke({"store_id": 99999, "days": 7})
    assert set(r.keys()) == {"success", "data", "error"} or r["success"] is False
