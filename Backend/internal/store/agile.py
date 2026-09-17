"""
Sprint store — Python port of the Go `store.SprintStore` type.

Uses asyncpg for database access and dataclasses for the domain models
(equivalent to the Go `domain` package types referenced in the original).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

class SprintState:
    ACTIVE = "active"
    # ... other states (planned, completed, etc.) as defined elsewhere


@dataclass
class BurndownPoint:
    date: str
    remaining: float
    remaining_tasks: int
    ideal: float


@dataclass
class SprintBurndown:
    sprint_id: uuid.UUID
    name: str
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    points: list[BurndownPoint] = field(default_factory=list)
    total_points: float = 0.0
    total_tasks: int = 0
    done_points: float = 0.0
    done_tasks: int = 0


@dataclass
class VelocityPoint:
    sprint_id: uuid.UUID
    name: str
    state: str
    committed: float
    completed: float
    committed_tasks: int
    completed_tasks: int


@dataclass
class AssigneeCapacity:
    user_id: Optional[uuid.UUID]
    display_name: str
    points: float
    tasks: int
    done_tasks: int


@dataclass
class SprintCapacity:
    sprint_id: uuid.UUID
    by_assignee: list[AssigneeCapacity] = field(default_factory=list)
    total_points: float = 0.0
    total_tasks: int = 0
    done_points: float = 0.0
    done_tasks: int = 0


@dataclass
class Sprint:
    id: uuid.UUID
    name: str
    state: str
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    # ... other fields as defined elsewhere


# ---------------------------------------------------------------------------
# SprintStore
# ---------------------------------------------------------------------------

class SprintStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_by_id(self, sprint_id: uuid.UUID) -> Sprint:
        """Equivalent of the Go `GetByID` used at the top of `Burndown`."""
        row = await self.pool.fetchrow(
            """
            SELECT id, name, state, start_date, end_date
            FROM sprints
            WHERE id = $1
            """,
            sprint_id,
        )
        if row is None:
            raise LookupError(f"sprint {sprint_id} not found")
        return Sprint(
            id=row["id"],
            name=row["name"],
            state=row["state"],
            start_date=row["start_date"],
            end_date=row["end_date"],
        )

    async def burndown(self, sprint_id: uuid.UUID) -> SprintBurndown:
        """
        Build the day-by-day remaining-work series for a sprint.

        A task counts as finished on the day it reached "done" — taken from
        its last status_changed activity event, falling back to updated_at
        for tasks created directly in that status. Sprints without explicit
        dates fall back to the window spanned by their tasks so the chart
        still renders.
        """
        sp = await self.get_by_id(sprint_id)

        b = SprintBurndown(
            sprint_id=sp.id,
            name=sp.name,
            start_date=sp.start_date,
            end_date=sp.end_date,
        )

        rows = await self.pool.fetch(
            """
            SELECT COALESCE(t.story_points, 0),
                   CASE WHEN t.status = 'done' THEN COALESCE(
                       (SELECT max(a.created_at) FROM activity_events a
                         WHERE a.task_id = t.id AND a.verb = 'status_changed'
                           AND a.meta->>'to' = 'done'),
                       t.updated_at) END,
                   t.created_at
            FROM tasks t
            WHERE t.sprint_id = $1
            """,
            sprint_id,
        )

        items: list[tuple[float, Optional[datetime]]] = []
        earliest: Optional[datetime] = None
        latest: Optional[datetime] = None

        for points, done_at, created_at in rows:
            points = float(points)
            items.append((points, done_at))
            b.total_points += points
            b.total_tasks += 1
            if done_at is not None:
                b.done_points += points
                b.done_tasks += 1
            if earliest is None or created_at < earliest:
                earliest = created_at
            if done_at is not None and (latest is None or done_at > latest):
                latest = done_at

        if not items:
            return b

        # Resolve the chart window.
        start = sp.start_date or earliest
        end = sp.end_date or latest

        now = datetime.now(timezone.utc)
        if end is None or end < start:
            end = now
        # Always show up to today for an in-flight sprint.
        if sp.state == SprintState.ACTIVE and end < now:
            end = now

        start = _truncate_to_day(start)
        end = _truncate_to_day(end)

        days = int((end - start).days) + 1
        if days < 1:
            days = 1
        if days > 180:  # guard against absurd ranges
            days = 180

        for i in range(days):
            day = start + timedelta(days=i)
            day_end = day + timedelta(days=1)

            remaining = 0.0
            remaining_tasks = 0
            for points, done_at in items:
                if done_at is None or not done_at < day_end:
                    remaining += points
                    remaining_tasks += 1

            if days > 1:
                ideal = b.total_points * (1 - i / (days - 1))
            else:
                ideal = 0.0

            b.points.append(
                BurndownPoint(
                    date=day.strftime("%Y-%m-%d"),
                    remaining=remaining,
                    remaining_tasks=remaining_tasks,
                    ideal=ideal,
                )
            )

        return b

    async def velocity(self, project_id: uuid.UUID) -> list[VelocityPoint]:
        """Committed vs completed story points for a project's sprints."""
        rows = await self.pool.fetch(
            """
            SELECT sp.id, sp.name, sp.state,
                   COALESCE(SUM(COALESCE(t.story_points, 0)), 0),
                   COALESCE(SUM(CASE WHEN t.status = 'done' THEN COALESCE(t.story_points, 0) ELSE 0 END), 0),
                   count(t.id),
                   count(t.id) FILTER (WHERE t.status = 'done')
            FROM sprints sp
            LEFT JOIN tasks t ON t.sprint_id = sp.id
            WHERE sp.project_id = $1
            GROUP BY sp.id, sp.name, sp.state, sp.position
            ORDER BY sp.position
            """,
            project_id,
        )

        return [
            VelocityPoint(
                sprint_id=row[0],
                name=row[1],
                state=row[2],
                committed=float(row[3]),
                completed=float(row[4]),
                committed_tasks=row[5],
                completed_tasks=row[6],
            )
            for row in rows
        ]

    async def capacity(self, sprint_id: uuid.UUID) -> SprintCapacity:
        """Summarise a sprint's load per assignee."""
        c = SprintCapacity(sprint_id=sprint_id)

        rows = await self.pool.fetch(
            """
            SELECT t.assignee_id, COALESCE(u.display_name, u.email::text, 'Chưa gán'),
                   COALESCE(SUM(COALESCE(t.story_points, 0)), 0),
                   count(*),
                   count(*) FILTER (WHERE t.status = 'done')
            FROM tasks t
            LEFT JOIN users u ON u.id = t.assignee_id
            WHERE t.sprint_id = $1
            GROUP BY t.assignee_id, u.display_name, u.email
            ORDER BY SUM(COALESCE(t.story_points, 0)) DESC
            """,
            sprint_id,
        )

        for user_id, display_name, points, tasks, done_tasks in rows:
            a = AssigneeCapacity(
                user_id=user_id,
                display_name=display_name,
                points=float(points),
                tasks=tasks,
                done_tasks=done_tasks,
            )
            c.by_assignee.append(a)
            c.total_points += a.points
            c.total_tasks += a.tasks
            c.done_tasks += a.done_tasks

        c.done_points = await self.pool.fetchval(
            """
            SELECT COALESCE(SUM(CASE WHEN status='done' THEN COALESCE(story_points,0) ELSE 0 END), 0)
            FROM tasks WHERE sprint_id = $1
            """,
            sprint_id,
        )
        c.done_points = float(c.done_points)

        return c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_to_day(dt: datetime) -> datetime:
    """Equivalent of Go's `time.Truncate(24 * time.Hour)` for a UTC date."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)