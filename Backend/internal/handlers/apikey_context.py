from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from ..store.apikeys import ResolvedKey


_api_key_ctx: ContextVar[Optional[ResolvedKey]] = ContextVar(
    "taskora_api_key",
    default=None,
)


def set_api_key(key: ResolvedKey):
    return _api_key_ctx.set(key)


def reset_api_key(token) -> None:
    _api_key_ctx.reset(token)


def key_from_context() -> tuple[Optional[ResolvedKey], bool]:
    key = _api_key_ctx.get()
    return key, key is not None
