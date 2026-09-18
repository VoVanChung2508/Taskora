"""
GDPR data export + account anonymisation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg


class ExportError(Exception):
    pass


@dataclass
class UserProfile:
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime]


_EXPORT_SECTIONS: list[tuple[str, str]] = [
    (
        "workspaceMemberships",
        """
        SELECT w.name AS workspace, m.role, w.created_at
        FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
        WHERE m.user_id = $1
        """,
    ),
    (
        "assignedTasks",
        """
        SELECT t.title, t.status, t.priority, p.key AS project, t.created_at
        FROM tasks t JOIN projects p ON p.id = t.project_id
        WHERE t.assignee_id = $1
        """,
    ),
    (
        "reportedTasks",
        """
        SELECT t.title, t.status, p.key AS project, t.created_at
        FROM tasks t JOIN projects p ON p.id = t.project_id
        WHERE t.reporter_id = $1
        """,
    ),
    (
        "comments",
        """
        SELECT c.body, t.title AS task, c.created_at
        FROM comments c JOIN tasks t ON t.id = c.task_id
        WHERE c.author_id = $1
        """,
    ),
    (
        "worklogs",
        """
        SELECT w.minutes, w.note, w.logged_on, w.source, w.state, t.title AS task
        FROM worklogs w JOIN tasks t ON t.id = w.task_id
        WHERE w.user_id = $1
        """,
    ),
    (
        "chatMessages",
        """
        SELECT m.body, c.name AS channel, m.created_at
        FROM chat_messages m JOIN chat_channels c ON c.id = m.channel_id
        WHERE m.author_id = $1
        """,
    ),
    (
        "notifications",
        """
        SELECT type, title, body, read_at, created_at
        FROM notifications WHERE user_id = $1
        """,
    ),
    (
        "sessions",
        """
        SELECT device, ip, last_seen, expires_at, created_at, revoked_at
        FROM user_sessions WHERE user_id = $1
        """,
    ),
    (
        "activity",
        """
        SELECT verb, meta, created_at
        FROM activity_events WHERE actor_id = $1
        """,
    ),
]


class UserStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_by_id(self, user_id: uuid.UUID) -> UserProfile:
        row = await self.pool.fetchrow(
            """
            SELECT id, email::text, display_name, is_active, created_at, last_login_at
            FROM users WHERE id = $1
            """,
            user_id,
        )
        if row is None:
            raise ExportError("user not found")
        return UserProfile(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    async def export_data(self, user_id: uuid.UUID) -> dict[str, Any]:
        out: dict[str, Any] = {
            "exportedAt": datetime.now(timezone.utc),
            "notice": (
                "Bản xuất dữ liệu cá nhân theo GDPR. "
                "Bao gồm hồ sơ, thành viên workspace, công việc, bình luận, "
                "worklog, thông báo và phiên đăng nhập."
            ),
        }

        profile = await self.get_by_id(user_id)
        out["profile"] = {
            "id": profile.id,
            "email": profile.email,
            "displayName": profile.display_name,
            "isActive": profile.is_active,
            "createdAt": profile.created_at,
            "lastLoginAt": profile.last_login_at,
        }

        for key, query in _EXPORT_SECTIONS:
            try:
                rows = await self.pool.fetch(query, user_id)
            except Exception as exc:
                raise ExportError(f"export {key}: {exc}") from exc
            out[key] = [dict(row) for row in rows]

        return out

    async def anonymise_account(self, user_id: uuid.UUID) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for query in (
                    "DELETE FROM notifications WHERE user_id = $1",
                    "DELETE FROM user_sessions WHERE user_id = $1",
                    "DELETE FROM active_timers WHERE user_id = $1",
                    "DELETE FROM chat_reads WHERE user_id = $1",
                    "DELETE FROM team_members WHERE user_id = $1",
                    "DELETE FROM workspace_members WHERE user_id = $1",
                    "DELETE FROM project_members WHERE user_id = $1",
                    "DELETE FROM user_rates WHERE user_id = $1",
                ):
                    await conn.execute(query, user_id)

                for query in (
                    "UPDATE tasks SET assignee_id = NULL WHERE assignee_id = $1",
                    "UPDATE tasks SET reporter_id = NULL WHERE reporter_id = $1",
                    "UPDATE comments SET author_id = NULL WHERE author_id = $1",
                    "UPDATE chat_messages SET author_id = NULL WHERE author_id = $1",
                    "UPDATE activity_events SET actor_id = NULL WHERE actor_id = $1",
                ):
                    await conn.execute(query, user_id)

                placeholder = f"deleted-{str(user_id)[:8]}@anonymised.invalid"
                await conn.execute(
                    """
                    UPDATE users SET
                        email = $2,
                        display_name = 'Người dùng đã xoá',
                        avatar_url = '',
                        azure_oid = 'deleted|' || $1::text,
                        is_active = FALSE,
                        is_system_admin = FALSE,
                        totp_secret = '',
                        totp_enabled = FALSE,
                        recovery_codes = '[]'
                    WHERE id = $1
                    """,
                    user_id,
                    placeholder,
                )
