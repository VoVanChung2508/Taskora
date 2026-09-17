"""
GDPR data export + account anonymisation — Python port of the Go
`store.UserStore.ExportData` and `store.UserStore.AnonymiseAccount` methods.

Uses asyncpg for database access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg


class ExportError(Exception):
    """Raised when a section of the GDPR export query fails."""


# ---------------------------------------------------------------------------
# Domain model (minimal shape needed by ExportData; adjust to match your
# actual `domain.User` / `UserStore.GetByID` return type)
# ---------------------------------------------------------------------------

@dataclass
class UserProfile:
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime]


# A section of the export: a JSON key and the query that produces its rows.
# (Equivalent of the anonymous `struct{ key, query string }` slice in Go.)
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


# ---------------------------------------------------------------------------
# UserStore
# ---------------------------------------------------------------------------

class UserStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_by_id(self, user_id: uuid.UUID) -> UserProfile:
        """
        Placeholder — not defined in the provided Go source (referenced as
        `s.GetByID`). Implement to match your actual UserStore.GetByID.
        """
        raise NotImplementedError(
            "get_by_id is referenced by export_data but was not defined in "
            "the provided Go source — implement it to match UserStore.GetByID."
        )

    async def export_data(self, user_id: uuid.UUID) -> dict[str, Any]:
        """
        Collect every record tied to a user for a GDPR data export.

        Values are returned as plain dicts so the JSON mirrors the database
        rows without needing a type per table.
        """
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
            except Exception as e:
                raise ExportError(f"export {key}: {e}") from e

            # Each row becomes a plain dict keyed by column name, mirroring
            # the Go code's use of `rows.FieldDescriptions()` + `rows.Values()`.
            items = [dict(row) for row in rows]
            out[key] = items

        return out

    async def anonymise_account(self, user_id: uuid.UUID) -> None:
        """
        Scrub personal data while keeping project history intact.

        Deleting the user row outright would cascade away tasks and
        comments the team still needs, so instead the identity is replaced
        with a placeholder and the account is deactivated. Authored content
        stays, but is no longer attributable.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Personal content that carries no project value is removed
                # outright.
                for q in (
                    "DELETE FROM notifications WHERE user_id = $1",
                    "DELETE FROM user_sessions WHERE user_id = $1",
                    "DELETE FROM active_timers WHERE user_id = $1",
                    "DELETE FROM chat_reads WHERE user_id = $1",
                    "DELETE FROM team_members WHERE user_id = $1",
                    "DELETE FROM workspace_members WHERE user_id = $1",
                    "DELETE FROM project_members WHERE user_id = $1",
                    "DELETE FROM user_rates WHERE user_id = $1",
                ):
                    await conn.execute(q, user_id)

                # Detach authored content so history survives without
                # naming the person.
                for q in (
                    "UPDATE tasks SET assignee_id = NULL WHERE assignee_id = $1",
                    "UPDATE tasks SET reporter_id = NULL WHERE reporter_id = $1",
                    "UPDATE comments SET author_id = NULL WHERE author_id = $1",
                    "UPDATE chat_messages SET author_id = NULL WHERE author_id = $1",
                    "UPDATE activity_events SET actor_id = NULL WHERE actor_id = $1",
                ):
                    await conn.execute(q, user_id)

                # Scrub the identity itself. The email is replaced with a
                # unique placeholder so the UNIQUE constraint still holds.
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