"""
Chat store — Python port of the Go `store.ChatStore` type.

Handles persistence for project chat channels and messages, using asyncpg
for database access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

@dataclass
class ChatChannel:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    created_at: datetime
    unread: int = 0


@dataclass
class ChatMessage:
    id: uuid.UUID
    channel_id: uuid.UUID
    author_id: Optional[uuid.UUID]
    author_name: str
    author_email: str
    body: str
    created_at: datetime


# ---------------------------------------------------------------------------
# ChatStore
# ---------------------------------------------------------------------------

class ChatStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_channels(self, project_id: uuid.UUID, user_id: uuid.UUID) -> list[ChatChannel]:
        """Return a project's channels with the caller's unread count."""
        rows = await self.pool.fetch(
            """
            SELECT c.id, c.project_id, c.name, c.created_at,
                   (SELECT count(*) FROM chat_messages m
                     WHERE m.channel_id = c.id
                       AND m.author_id IS DISTINCT FROM $2
                       AND m.created_at > COALESCE(
                           (SELECT r.last_read FROM chat_reads r
                             WHERE r.channel_id = c.id AND r.user_id = $2),
                           'epoch'::timestamptz))
            FROM chat_channels c
            WHERE c.project_id = $1
            ORDER BY c.created_at
            """,
            project_id,
            user_id,
        )

        return [
            ChatChannel(
                id=row[0],
                project_id=row[1],
                name=row[2],
                created_at=row[3],
                unread=row[4],
            )
            for row in rows
        ]

    async def create_channel(self, project_id: uuid.UUID, name: str) -> ChatChannel:
        """Add a channel to a project."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO chat_channels (project_id, name) VALUES ($1, $2)
            RETURNING id, project_id, name, created_at
            """,
            project_id,
            name,
        )
        return ChatChannel(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            created_at=row["created_at"],
        )

    async def channel_project(self, channel_id: uuid.UUID) -> uuid.UUID:
        """Return the project a channel belongs to, for access checks."""
        row = await self.pool.fetchrow(
            "SELECT project_id FROM chat_channels WHERE id = $1",
            channel_id,
        )
        if row is None:
            raise ErrNotFound
        return row["project_id"]

    async def delete_channel(self, channel_id: uuid.UUID) -> None:
        """Remove a channel and its messages (cascade)."""
        result = await self.pool.execute(
            "DELETE FROM chat_channels WHERE id = $1",
            channel_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def list_messages(self, channel_id: uuid.UUID, limit: int) -> list[ChatMessage]:
        """
        Return the most recent messages in a channel, oldest first.

        (Pagination via a `before` cursor is not implemented here, matching
        the Go source, whose docstring mentions it but whose signature does
        not yet take the parameter.)
        """
        if limit <= 0 or limit > 200:
            limit = 100

        rows = await self.pool.fetch(
            """
            SELECT m.id, m.channel_id, m.author_id,
                   COALESCE(u.display_name, ''), COALESCE(u.email::text, ''),
                   m.body, m.created_at
            FROM chat_messages m
            LEFT JOIN users u ON u.id = m.author_id
            WHERE m.channel_id = $1
            ORDER BY m.created_at DESC
            LIMIT $2
            """,
            channel_id,
            limit,
        )

        # Collected newest-first, then reversed so the UI renders chronologically.
        msgs = [
            ChatMessage(
                id=row[0],
                channel_id=row[1],
                author_id=row[2],
                author_name=row[3],
                author_email=row[4],
                body=row[5],
                created_at=row[6],
            )
            for row in rows
        ]
        msgs.reverse()
        return msgs

    async def post_message(self, channel_id: uuid.UUID, author_id: uuid.UUID, body: str) -> ChatMessage:
        """Append a message and return it with author details."""
        row = await self.pool.fetchrow(
            """
            WITH ins AS (
                INSERT INTO chat_messages (channel_id, author_id, body)
                VALUES ($1, $2, $3)
                RETURNING id, channel_id, author_id, body, created_at
            )
            SELECT ins.id, ins.channel_id, ins.author_id,
                   COALESCE(u.display_name, ''), COALESCE(u.email::text, ''),
                   ins.body, ins.created_at
            FROM ins LEFT JOIN users u ON u.id = ins.author_id
            """,
            channel_id,
            author_id,
            body,
        )
        return ChatMessage(
            id=row[0],
            channel_id=row[1],
            author_id=row[2],
            author_name=row[3],
            author_email=row[4],
            body=row[5],
            created_at=row[6],
        )

    async def mark_read(self, channel_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Record that the user has read a channel up to now."""
        await self.pool.execute(
            """
            INSERT INTO chat_reads (channel_id, user_id, last_read)
            VALUES ($1, $2, now())
            ON CONFLICT (channel_id, user_id) DO UPDATE SET last_read = now()
            """,
            channel_id,
            user_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0