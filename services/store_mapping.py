"""经营参谋外部门店与系统门店的确定性映射。"""
from __future__ import annotations

import re
from collections.abc import Iterable


def normalize_store_name(value: str | None) -> str:
    text = (value or "").lower().replace("\\n", "")
    return re.sub(r"[\s·•・()（）【】\[\]—_\-/\\,:：，。'\"]+", "", text)


def _brand_key(value: str | None) -> str:
    text = normalize_store_name(value)
    if "鬼十八" in text:
        return "鬼十八"
    if "bbboom" in text or ("bb" in text and "boom" in text):
        return "bbboom"
    if "异时刻" in text or "xcape" in text:
        return "异时刻"
    if "迷之好玩" in text:
        return "迷之好玩"
    return ""


def match_store(external_name: str | None, stores: Iterable[object]) -> object | None:
    """先按完整名称匹配，再按品牌 + 门店关键词唯一匹配；歧义时返回 None。"""
    candidates = list(stores)
    target = normalize_store_name(external_name)
    exact = [s for s in candidates if normalize_store_name(getattr(s, "store_name", "")) == target]
    if len(exact) == 1:
        return exact[0]

    brand = _brand_key(external_name)
    fuzzy: list[object] = []
    for store in candidates:
        if brand and _brand_key(getattr(store, "store_name", "")) != brand:
            continue
        keywords = {
            normalize_store_name(getattr(store, "search_keyword", "")),
            normalize_store_name(getattr(store, "budget_keyword", "")),
        }
        keywords.discard("")
        if keywords and any(k in target for k in keywords):
            fuzzy.append(store)
    return fuzzy[0] if len(fuzzy) == 1 else None


def build_platform_mapping(report_rows: Iterable[dict], stores: Iterable[object]) -> tuple[dict[int, int], list[str]]:
    """返回 {外部门店ID: 内部门店ID} 及未匹配门店名。"""
    store_list = list(stores)
    mapping: dict[int, int] = {}
    unmatched: set[str] = set()
    seen: set[tuple[int, str]] = set()
    for row in report_rows:
        external_id = int(row.get("store_id") or 0)
        external_name = str(row.get("store_name") or "").strip()
        if external_name.lower() in {"nan", "none", "null"}:
            external_name = ""
        key = (external_id, external_name)
        if not external_id or not external_name or key in seen:
            continue
        seen.add(key)
        store = match_store(external_name, store_list)
        if store is None:
            unmatched.add(external_name or str(external_id))
            continue
        mapping[external_id] = int(getattr(store, "id"))
    return mapping, sorted(unmatched)
