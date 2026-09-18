from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException

from ..auth.session import get_user_id

if TYPE_CHECKING:
    from .handlers import Handlers


def _current_user_id() -> uuid.UUID:
    """Lấy ID của user hiện đang được xác thực."""
    user_id, ok = get_user_id()

    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthenticated",
                "message": "missing authenticated user",
            },
        )

    return user_id


async def list_notifications(self: "Handlers") -> dict:
    """Trả về notification gần đây và số lượng chưa đọc."""
    user_id = _current_user_id()

    try:
        items, unread = await self.store.notifications.list_for_user(
            user_id,
            50,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "list_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "notifications": items,
        "unread": unread,
    }


async def mark_all_notifications_read(self: "Handlers") -> dict:
    """Đánh dấu tất cả notification của user là đã đọc."""
    user_id = _current_user_id()

    try:
        await self.store.notifications.mark_all_read(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "update_failed",
                "message": str(exc),
            },
        ) from exc

    return {"ok": True}


async def mark_notification_read(
    self: "Handlers",
    notif_id: uuid.UUID,
) -> dict:
    """Đánh dấu một notification là đã đọc."""
    user_id = _current_user_id()

    try:
        await self.store.notifications.mark_read(
            user_id,
            notif_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "update_failed",
                "message": str(exc),
            },
        ) from exc

    return {"ok": True}