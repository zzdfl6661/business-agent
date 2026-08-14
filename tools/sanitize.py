"""
错误信息脱敏（#8）
==================
问题：工具失败 `str(exc)` 可能含 SQL 细节（表名/字段/连接串）或敏感值（password/token），
直接暴露给 LLM / 前端 / 审计。

方案：统一 sanitize_error() ——
- 掩码敏感键值（password / api_key / secret / token / 连接串等）
- 截断长度（默认 200 字符），避免 SQL 全量外泄
"""
from __future__ import annotations

import re

# 敏感键值：如 password='xxx' / "api_key": "sk-..." / token=abcd
_SENSITIVE_KV = re.compile(
    r"(?i)(password|passwd|pwd|api[_-]?key|apikey|secret|access[_-]?token|token|authorization)"
    r"(['\"]?\s*[:=]\s*['\"]?)([^,;}\s'\"]{1,96})"
)
# 连接串中的凭据：mysql+pymysql://user:pass@host
_DSN_CRED = re.compile(r"(?i)(://[^:/@\s]+:)([^@/\s]+)(@)")

MAX_ERROR_LEN = 200


def sanitize_error(msg: str, max_len: int = MAX_ERROR_LEN) -> str:
    """对错误文本脱敏：掩码敏感键值 + 连接串凭据 + 截断。"""
    if not msg:
        return ""
    text = str(msg)
    text = _SENSITIVE_KV.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    text = _DSN_CRED.sub(lambda m: m.group(1) + "***" + m.group(3), text)
    if len(text) > max_len:
        text = text[:max_len] + "…(已截断)"
    return text
