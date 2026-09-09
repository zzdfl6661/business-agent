"""
request_id 贯穿（#8）
=====================
- 每个 API 请求生成唯一 request_id（uuid 前 12 位）
- contextvars 存储，自动传播到 threadpool/子任务（asyncio.to_thread 复制 context）
- 日志 formatter 通过 RequestIdFilter 注入；审计 payload 显式携带
- operator_id 同样使用 contextvars 传播，供审批审计记录调用方
"""
from __future__ import annotations

import contextvars
import logging
import uuid

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_operator_id: contextvars.ContextVar[str] = contextvars.ContextVar("operator_id", default="")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(rid: str):
    return _request_id.set(rid)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def get_operator_id() -> str:
    return _operator_id.get()


def set_operator_id(operator_id: str):
    return _operator_id.set(operator_id)


def reset_operator_id(token) -> None:
    _operator_id.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class RequestIdFilter(logging.Filter):
    """向 log record 注入 request_id 字段（formatter 使用 %(request_id)s）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True
