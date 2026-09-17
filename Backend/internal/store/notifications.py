"""
Notification store — Python port of the Go `store.NotificationStore` type.

Handles persistence for in-app notifications, using asyncpg for database
access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.Notification` type)
# ---------------------------------------------------------------------------

@dataclass
class Notification:
    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    title: str
    body: str
    task_id: Optional[uuid.UUID]
    link: str
    read_at: Optional[datetime]
    created_at: datetime


# ---------------------------------------------------------------------------
# NotificationStore
# ---------------------------------------------------------------------------

class NotificationStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        user_id: uuid.UUID,
        ntype: str,
        title: str,
        body: str,
        task_id: Optional[uuid.UUID],
        link: str,
    ) -> None:
        """
        Insert a notification for a user.

        `link` is the frontend path to open when the notification is
        clicked. It is computed at write time because the notification
        list must not need a lookup per row just to become clickable.
        """
        await self.pool.execute(
            """
            INSERT INTO notifications (user_id, type, title, body, task_id, link)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            user_id,
            ntype,
            title,
            body,
            task_id,
            link,
        )

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        limit: int,
    ) -> tuple[list[Notification], int]:
        """Return a user's notifications (newest first) + unread count."""
        rows = await self.pool.fetch(
            """
            SELECT id, user_id, type, title, body, task_id, link, read_at, created_at
            FROM notifications WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2
            """,
            user_id,
            limit,
        )

        out: list[Notification] = []
        for row in rows:
            out.append(
                Notification(
                    id=row["id"],
                    user_id=row["user_id"],
                    type=row["type"],
                    title=row["title"],
                    body=row["body"],
                    task_id=row["task_id"],
                    link=row["link"],
                    read_at=row["read_at"],
                    created_at=row["created_at"],
                )
            )

        unread = await self.pool.fetchval(
            "SELECT count(*) FROM notifications WHERE user_id=$1 AND read_at IS NULL",
            user_id,
        )

        return out, unread

    async def mark_all_read(self, user_id: uuid.UUID) -> None:
        """Mark all of a user's notifications as read."""
        await self.pool.execute(
            "UPDATE notifications SET read_at=now() WHERE user_id=$1 AND read_at IS NULL",
            user_id,
        )

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> None:
        """Mark a single notification read (scoped to the owner)."""
        await self.pool.execute(
            "UPDATE notifications SET read_at=now() WHERE id=$1 AND user_id=$2",
            notification_id,
            user_id,
        )