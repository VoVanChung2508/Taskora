import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


class TimerRunningError(Exception):
    """Raised when a user starts a timer while another is live."""
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


@dataclass
class Worklog:
    # Placeholder shape; align with the actual domain.Worklog fields
    # returned by WorklogStore.Add if they differ.
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    minutes: int
    note: str
    source: str
    logged_at: datetime


class WorklogStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def start_timer(
        self, user_id: uuid.UUID, task_id: uuid.UUID, note: str
    ) -> ActiveTimer:
        """Begins a stopwatch on a task. Only one timer per user may run at
        a time; starting another raises TimerRunningError."""
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
        return timer

    async def active_timer(self, user_id: uuid.UUID) -> Optional[ActiveTimer]:
        """Returns the user's running timer, or None when none is running."""
        row = await self.pool.fetchrow(
            """
            SELECT a.user_id, a.task_id, t.title, p.id, p.key, a.note, a.started_at
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
        now = datetime.now(started_at.tzinfo) if started_at.tzinfo else datetime.now()
        elapsed_secs = int((now - started_at).total_seconds())

        return ActiveTimer(
            user_id=row[0],
            task_id=row[1],
            task_title=row[2],
            project_id=row[3],
            project_key=row[4],
            note=row[5],
            started_at=started_at,
            elapsed_secs=elapsed_secs,
        )

    async def stop_timer(self, user_id: uuid.UUID, note: str) -> Worklog:
        """Ends the running timer and converts it into a worklog. Elapsed
        time is rounded to the nearest minute with a floor of 1, so very
        short sessions still record something. Raises NotFoundError when no
        timer is running."""
        row = await self.pool.fetchrow(
            """
            DELETE FROM active_timers WHERE user_id = $1
            RETURNING task_id, started_at, note
            """,
            user_id,
        )
        if row is None:
            raise NotFoundError

        task_id = row["task_id"]
        started_at = row["started_at"]
        stored_note = row["note"]

        now = datetime.now(started_at.tzinfo) if started_at.tzinfo else datetime.now()
        minutes = round((now - started_at).total_seconds() / 60.0)
        if minutes < 1:
            minutes = 1

        if not note:
            note = stored_note

        return await self.add(task_id, user_id, minutes, note, "timer", datetime.now(timezone.utc))

    async def cancel_timer(self, user_id: uuid.UUID) -> None:
        """Discards the running timer without logging any time."""
        result = await self.pool.execute(
            "DELETE FROM active_timers WHERE user_id = $1", user_id
        )
        if result.split()[-1] == "0":
            raise NotFoundError

    async def add(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        minutes: int,
        note: str,
        source: str,
        logged_at: datetime,
    ) -> Worklog:
        """Inserts a worklog entry. Signature mirrors WorklogStore.Add from
        the Go codebase — fill in with the actual implementation/columns
        once that file is shared."""
        raise NotImplementedError(
            "Add(task_id, user_id, minutes, note, source, logged_at) needs the "
            "original WorklogStore.Add Go source to translate accurately."
        )