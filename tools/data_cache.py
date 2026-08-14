"""
数据工具 TTL 缓存（#7）
======================
问题：每个请求实时聚合近 7 天 23.3 万订单（无缓存），反复问答重复查库。

方案：
- 昂贵查询（get_sales_data / get_campaign_data / 市场数据）结果按参数缓存，默认 TTL 60s；
- `/api/workflow/refresh` 成功后调用 `invalidate_data_cache()` 立即失效（保证刷新生效）；
- 线程安全（threading.Lock）；进程内内存缓存（无 Redis 依赖，单实例部署足够）；
- 返回前 deepcopy，避免调用方修改污染缓存。
"""
from __future__ import annotations

import copy
import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_TTL = 60  # 秒

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _key(tool: str, params: dict) -> str:
    """缓存键：工具名 + 规范化参数（None/空串省略，保证等价调用命中同一键）。"""
    norm = []
    for k in sorted(params):
        v = params[k]
        if v is None or v == "":
            continue
        norm.append(f"{k}={v}")
    return f"{tool}:{','.join(norm)}"


def get(tool: str, params: dict, ttl: int = DEFAULT_TTL):
    """命中返回 deepcopy 结果；未命中/过期返回 None。"""
    key = _key(tool, params)
    with _lock:
        item = _cache.get(key)
        if item and time.time() - item[0] < ttl:
            return copy.deepcopy(item[1])
        if item:  # 过期清理
            _cache.pop(key, None)
    return None


def set(tool: str, params: dict, value) -> None:
    """写入缓存（deepcopy 存储，防外部修改）。"""
    key = _key(tool, params)
    with _lock:
        _cache[key] = (time.time(), copy.deepcopy(value))


def invalidate_data_cache() -> None:
    """全部失效：/api/workflow/refresh 成功后调用，保证新数据立即可见。"""
    with _lock:
        n = len(_cache)
        _cache.clear()
    if n:
        logger.info("数据工具缓存已失效（%s 项）", n)


def cache_stats() -> dict:
    with _lock:
        return {"entries": len(_cache), "ttl": DEFAULT_TTL}
