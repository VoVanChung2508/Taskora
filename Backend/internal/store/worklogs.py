import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


@dataclass
class Worklog:
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    minutes: int
    note: str
    logged_on: date
    source: str
    state: str
    created_at: datetime


@dataclass
class TimesheetEntry:
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    minutes: int
    note: str
    logged_on: date
    source: str
    state: str
    created_at: datetime
    task_title: str
    project_id: uuid.UUID
    project_name: str
    project_key: str
    user_display_name: str = ""
    user_email: str = ""


class WorklogStore:
    """Handles persistence for time entries."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def add(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        minutes: int,
        note: str,
        source: str,
        logged_on: date,
    ) -> Worklog:
        """Inserts a worklog entry."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO worklogs (task_id, user_id, minutes, note, source, logged_on)
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING id, task_id, user_id, minutes, note, logged_on, source, state, created_at
            """,
            task_id,
            user_id,
            minutes,
            note,
            source,
            logged_on,
        )
        return Worklog(
            id=row["id"],
            task_id=row["task_id"],
            user_id=row["user_id"],
            minutes=row["minutes"],
            note=row["note"],
            logged_on=row["logged_on"],
            source=row["source"],
            state=row["state"],
            created_at=row["created_at"],
        )

    async def list_by_task(self, task_id: uuid.UUID) -> list[Worklog]:
        """Returns worklogs on a task, newest-first."""
        rows = await self.pool.fetch(
            """
            SELECT id, task_id, user_id, minutes, note, logged_on, source, state, created_at
            FROM worklogs WHERE task_id=$1 ORDER BY logged_on DESC, created_at DESC
            """,
            task_id,
        )
        return [
            Worklog(
                id=r["id"],
                task_id=r["task_id"],
                user_id=r["user_id"],
                minutes=r["minutes"],
                note=r["note"],
                logged_on=r["logged_on"],
                source=r["source"],
                state=r["state"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def _timesheet_query(
        self, where_clause: str, order_by: str, *params
    ) -> list[TimesheetEntry]:
        rows = await self.pool.fetch(
            f"""
            SELECT w.id, w.task_id, w.user_id, w.minutes, w.note, w.logged_on, w.source, w.state, w.created_at,
                   t.title, p.id, p.name, p.key, u.display_name, u.email
            FROM worklogs w
            JOIN tasks t ON t.id = w.task_id
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN users u ON u.id = w.user_id
            WHERE {where_clause}
            ORDER BY {order_by}
            """,
            *params,
        )
        out = []
        for r in rows:
            out.append(
                TimesheetEntry(
                    id=r[0],
                    task_id=r[1],
                    user_id=r[2],
                    minutes=r[3],
                    note=r[4],
                    logged_on=r[5],
                    source=r[6],
                    state=r[7],
                    created_at=r[8],
                    task_title=r[9],
                    project_id=r[10],
                    project_name=r[11],
                    project_key=r[12],
                    user_display_name=r[13] if r[13] is not None else "",
                    user_email=r[14] if r[14] is not None else "",
                )
            )
        return out

    async def timesheet_for_user(
        self, user_id: uuid.UUID, from_: date, to: date
    ) -> list[TimesheetEntry]:
        """Returns a user's worklogs in a date range with task/project
        context (for the timesheet grid)."""
        return await self._timesheet_query(
            "w.user_id=$1 AND w.logged_on BETWEEN $2 AND $3",
            "w.logged_on",
            user_id,
            from_,
            to,
        )

    async def timesheet_for_project(
        self, project_id: uuid.UUID, from_: date, to: date
    ) -> list[TimesheetEntry]:
        """Returns all worklogs for a project in a date range."""
        return await self._timesheet_query(
            "p.id=$1 AND w.logged_on BETWEEN $2 AND $3",
            "w.user_id, w.logged_on",
            project_id,
            from_,
            to,
        )

    async def submit_range(self, user_id: uuid.UUID, from_: date, to: date) -> int:
        """Transitions a user's draft worklogs in a range to submitted."""
        result = await self.pool.execute(
            """
            UPDATE worklogs SET state='submitted'
            WHERE user_id=$1 AND logged_on BETWEEN $2 AND $3 AND state='draft'
            """,
            user_id,
            from_,
            to,
        )
        # result looks like "UPDATE <n>"
        return int(result.split()[-1])

    async def get_by_id(self, id: uuid.UUID) -> Worklog:
        """Returns a single worklog."""
        row = await self.pool.fetchrow(
            """
            SELECT id, task_id, user_id, minutes, note, logged_on, source, state, created_at
            FROM worklogs WHERE id=$1
            """,
            id,
        )
        if row is None:
            raise NotFoundError
        return Worklog(
            id=row["id"],
            task_id=row["task_id"],
            user_id=row["user_id"],
            minutes=row["minutes"],
            note=row["note"],
            logged_on=row["logged_on"],
            source=row["source"],
            state=row["state"],
            created_at=row["created_at"],
        )

    async def set_state(self, id: uuid.UUID, state: str) -> None:
        """Updates a worklog's approval state."""
        await self.pool.execute(
            "UPDATE worklogs SET state=$2::worklog_state WHERE id=$1", id, state
        )