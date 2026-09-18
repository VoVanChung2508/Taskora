from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried timer does not exist."""
    pass


class TimerRunningError(Exception):
    """Raised when a user starts a timer while another is already running."""
    pass


@dataclass
class ActiveTimer:
    user_id: uuid.UUID
    task_id: uuid.UUID
    task_title: str
    project_id: uuid.UUID
    project_key: str
    note: str
    started_at: datetime
    elapsed_secs: int = 0


class WorklogStore:
    """
    Timer-specific WorklogStore mixin.

    The aggregate store combines this class with worklogs.WorklogStore. The
    latter provides add(), while this mixin provides stopwatch operations.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def start_timer(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        note: str,
    ) -> ActiveTimer:
        result = await self.pool.execute(
            """
            INSERT INTO active_timers (user_id, task_id, note)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
            task_id,
            note,
        )

        if result.split()[-1] == "0":
            raise TimerRunningError

        timer = await self.active_timer(user_id)
        if timer is None:
            raise NotFoundError
        return timer

    async def active_timer(
        self,
        user_id: uuid.UUID,
    ) -> Optional[ActiveTimer]:
        row = await self.pool.fetchrow(
            """
            SELECT
                a.user_id,
                a.task_id,
                t.title,
                p.id,
                p.key,
                a.note,
                a.started_at
            FROM active_timers a
            JOIN tasks t ON t.id = a.task_id
            JOIN projects p ON p.id = t.project_id
            WHERE a.user_id = $1
            """,
            user_id,
        )

        if row is None:
            return None

        started_at = row["started_at"]
        now = (
            datetime.now(started_at.tzinfo)
            if started_at.tzinfo
            else datetime.now()
        )

        return ActiveTimer(
            user_id=row[0],
            task_id=row[1],
            task_title=row[2],
            project_id=row[3],
            project_key=row[4],
            note=row[5],
            started_at=started_at,
            elapsed_secs=max(
                0,
                int((now - started_at).total_seconds()),
            ),
        )

    async def stop_timer(
        self,
        user_id: uuid.UUID,
        note: str,
    ):
        row = await self.pool.fetchrow(
            """
            DELETE FROM active_timers
            WHERE user_id = $1
            RETURNING task_id, started_at, note
            """,
            user_id,
        )

        if row is None:
            raise NotFoundError

        task_id = row["task_id"]
        started_at = row["started_at"]
        stored_note = row["note"]

        now = (
            datetime.now(started_at.tzinfo)
            if started_at.tzinfo
            else datetime.now()
        )

        # Go math.Round semantics for positive elapsed time.
        seconds = max(0.0, (now - started_at).total_seconds())
        minutes = int(seconds / 60.0 + 0.5)
        if minutes < 1:
            minutes = 1

        if not note:
            note = stored_note

        # add() comes from the core worklogs.WorklogStore in the aggregate
        # multiple-inheritance Store.
        return await self.add(
            task_id,
            user_id,
            minutes,
            note,
            "timer",
            datetime.now(timezone.utc).date(),
        )

    async def cancel_timer(
        self,
        user_id: uuid.UUID,
    ) -> None:
        result = await self.pool.execute(
            "DELETE FROM active_timers WHERE user_id = $1",
            user_id,
        )

        if result.split()[-1] == "0":
            raise NotFoundError
