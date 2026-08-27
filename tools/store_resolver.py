"""
门店名 → store_id 解析（#6）
============================
解决"XX店营业额"高频问题——旧实现 LLM 只能猜 store_id=1。

数据源优先级：
1. 数据库 `stores` 表（id 权威：seed 按 stores.json 顺序插入，id=序号）
2. 失败回退 stores.json（BIZ_STORES_JSON 或 data/stores.json，按顺序推断序号=store_id）

匹配规则（对用户问题）：
1. "N号门店 / N号店 / 第N店" 数字形式 → store_id=N（且 N 在门店列表内）
2. 门店名 / search_keyword 子串匹配（按名称长度降序，长名优先防误匹配）

解析失败返回 None，不影响主流程（LLM 继续用默认 store_id=1 或全部汇总）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config.settings import PROJECT_DIR, settings

logger = logging.getLogger(__name__)

# "1号门店" / "1号店" / "第1家店" / "1门店"（门店语境）
_NUM_RE = re.compile(r"(?:第|号)?\s*(\d{1,2})\s*(?:号)?\s*(?:门店|号店|店|分店|店铺)")
# 数字形式显式要求"号"或"第"，避免"哪家店"里的裸"店"误匹配
_NUM_RE_STRICT = re.compile(r"(?:第\s*(\d{1,2})\s*家|\s*(\d{1,2})\s*号\s*(?:门店|店|分店))")

_store_cache: list[dict] | None = None

# 用户常省略商场名称中的中间词，例如“上海大悦城”指“上海长风大悦城”。
# 仅对带有明确商业体后缀的地点短语启用，并且必须唯一命中，避免把
# “上海门店”之类的泛称错误归到某一家门店。
_LOCATION_SUFFIXES = (
    "大悦城", "天街", "万达", "来福士", "印象城", "广场", "世界商城", "太阳宫", "小南门",
)
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]+")


def _compact_text(value: str) -> str:
    """仅保留中文字符，供门店地点简称的确定性匹配使用。"""
    return "".join(_CHINESE_RE.findall(value or ""))


def _is_subsequence(needle: str, haystack: str) -> bool:
    """needle 的字符是否按顺序出现在 haystack 中（允许省略中间地点词）。"""
    if not needle or not haystack:
        return False
    pos = 0
    for char in needle:
        pos = haystack.find(char, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def _location_aliases(question: str) -> set[str]:
    """从问题中提取“上海大悦城”这类带商业体后缀的地点简称。"""
    compact_question = _compact_text(question)
    aliases: set[str] = set()
    for suffix in _LOCATION_SUFFIXES:
        start = 0
        while True:
            index = compact_question.find(suffix, start)
            if index < 0:
                break
            for prefix_len in range(1, min(4, index) + 1):
                alias = compact_question[index - prefix_len : index + len(suffix)]
                if len(alias) >= len(suffix) + 2:
                    aliases.add(alias)
            start = index + len(suffix)
    return aliases


def _load_stores() -> list[dict]:
    """加载门店字典 [{store_id, name, search_keyword, city}]（进程级缓存）。"""
    global _store_cache
    if _store_cache is not None:
        return _store_cache

    stores: list[dict] = []
    # 1) 数据库 Store 表（id 权威）
    try:
        from database.mysql import get_session_factory
        from database.models import Store
        from sqlalchemy import select

        with get_session_factory()() as session:
            rows = session.execute(
                select(Store.id, Store.store_name, Store.search_keyword, Store.city)
            ).all()
            stores = [
                {
                    "store_id": r.id,
                    "name": r.store_name or "",
                    "search_keyword": r.search_keyword or "",
                    "city": r.city or "",
                }
                for r in rows
            ]
        if stores:
            _store_cache = stores
            logger.info("门店解析数据源：DB stores 表（%s 家）", len(stores))
            return stores
    except Exception as exc:  # noqa: BLE001 库不可用时回退 JSON
        logger.warning("门店解析读 DB 失败（回退 stores.json）：%s", exc)

    # 2) stores.json 回退（序号=store_id，与 seed 顺序约定一致）
    try:
        candidates = []
        if settings.stores_json:
            candidates.append(Path(settings.stores_json))
        candidates.append(PROJECT_DIR / "data" / "stores.json")
        for p in candidates:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                raw = data.get("stores", []) if isinstance(data, dict) else data
                stores = [
                    {
                        "store_id": i + 1,
                        "name": s.get("name", "") or "",
                        "search_keyword": s.get("search_keyword", "") or "",
                        "city": "",
                    }
                    for i, s in enumerate(raw)
                ]
                _store_cache = stores
                logger.info("门店解析数据源：stores.json（%s 家）", len(stores))
                return stores
    except Exception as exc:  # noqa: BLE001
        logger.warning("门店解析读 stores.json 失败：%s", exc)

    _store_cache = []
    return _store_cache


def reset_store_cache() -> None:
    """测试/热加载用。"""
    global _store_cache
    _store_cache = None


def resolve_store_id(question: str) -> int | None:
    """从问题中解析目标门店 store_id；无法确定返回 None。"""
    q = (question or "").strip()
    if not q:
        return None
    stores = _load_stores()
    if not stores:
        return None
    valid_ids = {s["store_id"] for s in stores}

    # 1) 数字门店号："1号门店 / 第2家店 / 3号店"
    m = _NUM_RE_STRICT.search(q)
    if m:
        num = int(m.group(1) or m.group(2) or 0)
        if num in valid_ids:
            return num
        # 号码超出门店列表 → 不强行匹配（可能指其他含义），继续名称匹配
    m = _NUM_RE.search(q)
    if m:
        num = int(m.group(1) or 0)
        if num in valid_ids:
            return num

    # 2) 名称子串：按名称长度降序，长名优先（防"正大"误匹配"正大光明"场景）
    for s in sorted(stores, key=lambda x: -max(len(x["name"] or ""), len(x["search_keyword"] or ""))):
        if s["name"] and s["name"] in q:
            return s["store_id"]
        if s["search_keyword"] and len(s["search_keyword"]) >= 2 and s["search_keyword"] in q:
            return s["store_id"]

    # 3) 地点简称：如“上海大悦城”唯一对应“上海长风大悦城店”。
    # 多个候选仍交由澄清流程处理，禁止猜测门店。
    candidates: set[int] = set()
    for alias in _location_aliases(q):
        for s in stores:
            name_and_keyword = _compact_text(" ".join((s.get("name") or "", s.get("search_keyword") or "")))
            # city 不是门店全名的一部分时，也要把“城市 + 门店名”作为一条候选。
            # 这能使有多个同商场门店时正确识别歧义，而不是偏向名称里恰好带城市的行。
            city_and_name = _compact_text(" ".join((s.get("city") or "", s.get("name") or "", s.get("search_keyword") or "")))
            if _is_subsequence(alias, name_and_keyword) or _is_subsequence(alias, city_and_name):
                candidates.add(s["store_id"])
    if len(candidates) == 1:
        return next(iter(candidates))
    return None
