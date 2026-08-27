"""将 data/stores.json 幂等同步到 stores 主数据表。

该脚本只更新门店主数据，不生成演示订单、商品或推广数据：
    python -m scripts.sync_store_master
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from config.settings import PROJECT_DIR, settings
from database.models import Store
from database.mysql import get_session_factory, init_db


def _source_path() -> Path:
    candidates = []
    if settings.stores_json:
        candidates.append(Path(settings.stores_json))
    candidates.append(PROJECT_DIR / "data" / "stores.json")
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("未找到 stores.json")


def main() -> int:
    source = _source_path()
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get("stores", [])
    if not isinstance(rows, list) or not rows:
        raise ValueError("stores.json 不包含有效 stores 列表")

    init_db()
    inserted = 0
    updated = 0
    with get_session_factory()() as session:
        existing = {
            row.store_code: row
            for row in session.execute(select(Store)).scalars().all()
        }
        for index, item in enumerate(rows, start=1):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            code = f"ST{index:04d}"
            values = {
                "store_name": name,
                "search_keyword": str(item.get("search_keyword") or "").strip() or None,
                "budget_keyword": str(item.get("budget_keyword") or "").strip() or None,
                "status": "active" if item.get("enabled", True) else "closed",
            }
            store = existing.get(code)
            if store is None:
                session.add(Store(id=index, store_code=code, **values))
                inserted += 1
            else:
                for key, value in values.items():
                    setattr(store, key, value)
                updated += 1
        session.commit()

    print(json.dumps({
        "success": True,
        "source": source.name,
        "inserted": inserted,
        "updated": updated,
        "total": inserted + updated,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
