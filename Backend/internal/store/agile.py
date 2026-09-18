"""
Sprint store — Python port of the Go `store.SprintStore` type.

Uses asyncpg for database access and dataclasses for the domain models
(equivalent to the Go `domain` package types referenced in the original).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import asyncpg


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class SprintState:
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"


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
    start_date: date | datetime | None
    end_date: date | datetime | None
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
    start_date: date | datetime | None
    end_date: date | datetime | None


# ---------------------------------------------------------------------------
# SprintStore
# ---------------------------------------------------------------------------

class SprintStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_by_id(self, sprint_id: uuid.UUID) -> Sprint:
        """Fetch a sprint used by analytics helpers."""
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

    async def burndown(
        self,
        sprint_id: uuid.UUID,
    ) -> SprintBurndown:
        """
        Build the day-by-day remaining-work series for a sprint.

        A task counts as finished on the day it reached "done".
        """
        sp = await self.get_by_id(sprint_id)

        burndown = SprintBurndown(
            sprint_id=sp.id,
            name=sp.name,
            start_date=sp.start_date,
            end_date=sp.end_date,
        )

        rows = await self.pool.fetch(
            """
            SELECT
                COALESCE(t.story_points, 0),

                CASE
                    WHEN t.status = 'done'
                    THEN COALESCE(
                        (
                            SELECT max(a.created_at)
                            FROM activity_events a
                            WHERE a.task_id = t.id
                              AND a.verb = 'status_changed'
                              AND a.meta->>'to' = 'done'
                        ),
                        t.updated_at
                    )
                END,

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

            normalized_done = _as_utc_datetime(done_at)
            normalized_created = _as_utc_datetime(created_at)

            items.append(
                (
                    points,
                    normalized_done,
                )
            )

            burndown.total_points += points
            burndown.total_tasks += 1

            if normalized_done is not None:
                burndown.done_points += points
                burndown.done_tasks += 1

            if normalized_created is not None:
                if (
                    earliest is None
                    or normalized_created < earliest
                ):
                    earliest = normalized_created

            if normalized_done is not None:
                if (
                    latest is None
                    or normalized_done > latest
                ):
                    latest = normalized_done

        if not items:
            return burndown

        # start_date/end_date của bảng sprints là DATE.
        # created_at/done_at là TIMESTAMP.
        # Chuẩn hóa tất cả thành UTC datetime trước khi tính toán.
        start = (
            _as_utc_datetime(sp.start_date)
            or earliest
        )

        end = (
            _as_utc_datetime(sp.end_date)
            or latest
        )

        # Có task thì created_at phải tồn tại, nhưng giữ guard an toàn.
        if start is None:
            start = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)

        if end is None or end < start:
            end = now

        # Sprint đang active luôn hiển thị tới ngày hiện tại.
        if (
            sp.state == SprintState.ACTIVE
            and end < now
        ):
            end = now

        start = _truncate_to_day(start)
        end = _truncate_to_day(end)

        days = (end - start).days + 1

        if days < 1:
            days = 1

        # Guard tránh range lỗi tạo response quá lớn.
        if days > 180:
            days = 180

        for i in range(days):
            day = start + timedelta(days=i)
            day_end = day + timedelta(days=1)

            remaining = 0.0
            remaining_tasks = 0

            for points, done_at in items:
                if (
                    done_at is None
                    or done_at >= day_end
                ):
                    remaining += points
                    remaining_tasks += 1

            if days > 1:
                ideal = (
                    burndown.total_points
                    * (1 - i / (days - 1))
                )
            else:
                ideal = 0.0

            burndown.points.append(
                BurndownPoint(
                    date=day.strftime("%Y-%m-%d"),
                    remaining=remaining,
                    remaining_tasks=remaining_tasks,
                    ideal=ideal,
                )
            )

        return burndown

    async def velocity(
        self,
        project_id: uuid.UUID,
    ) -> list[VelocityPoint]:
        """Committed vs completed story points for project sprints."""
        rows = await self.pool.fetch(
            """
            SELECT
                sp.id,
                sp.name,
                sp.state,

                COALESCE(
                    SUM(COALESCE(t.story_points, 0)),
                    0
                ),

                COALESCE(
                    SUM(
                        CASE
                            WHEN t.status = 'done'
                            THEN COALESCE(t.story_points, 0)
                            ELSE 0
                        END
                    ),
                    0
                ),

                count(t.id),
                count(t.id)
                    FILTER (WHERE t.status = 'done')

            FROM sprints sp

            LEFT JOIN tasks t
                ON t.sprint_id = sp.id

            WHERE sp.project_id = $1

            GROUP BY
                sp.id,
                sp.name,
                sp.state,
                sp.position

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

    async def capacity(
        self,
        sprint_id: uuid.UUID,
    ) -> SprintCapacity:
        """Summarise sprint load per assignee."""
        capacity = SprintCapacity(
            sprint_id=sprint_id
        )

        rows = await self.pool.fetch(
            """
            SELECT
                t.assignee_id,

                COALESCE(
                    u.display_name,
                    u.email::text,
                    'Chưa gán'
                ),

                COALESCE(
                    SUM(
                        COALESCE(
                            t.story_points,
                            0
                        )
                    ),
                    0
                ),

                count(*),

                count(*)
                    FILTER (
                        WHERE t.status = 'done'
                    )

            FROM tasks t

            LEFT JOIN users u
                ON u.id = t.assignee_id

            WHERE t.sprint_id = $1

            GROUP BY
                t.assignee_id,
                u.display_name,
                u.email

            ORDER BY
                SUM(
                    COALESCE(
                        t.story_points,
                        0
                    )
                ) DESC
            """,
            sprint_id,
        )

        for (
            user_id,
            display_name,
            points,
            tasks,
            done_tasks,
        ) in rows:
            assignee = AssigneeCapacity(
                user_id=user_id,
                display_name=display_name,
                points=float(points),
                tasks=tasks,
                done_tasks=done_tasks,
            )

            capacity.by_assignee.append(
                assignee
            )

            capacity.total_points += (
                assignee.points
            )

            capacity.total_tasks += (
                assignee.tasks
            )

            capacity.done_tasks += (
                assignee.done_tasks
            )

        done_points = await self.pool.fetchval(
            """
            SELECT COALESCE(
                SUM(
                    CASE
                        WHEN status = 'done'
                        THEN COALESCE(
                            story_points,
                            0
                        )
                        ELSE 0
                    END
                ),
                0
            )

            FROM tasks

            WHERE sprint_id = $1
            """,
            sprint_id,
        )

        capacity.done_points = float(
            done_points or 0
        )

        return capacity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_utc_datetime(
    value: date | datetime | None,
) -> datetime | None:
    """
    Normalize PostgreSQL DATE/TIMESTAMP values into UTC datetime.

    asyncpg returns PostgreSQL DATE as datetime.date, while task timestamps
    are datetime.datetime. Burndown calculations need one common type.
    """
    if value is None:
        return None

    # datetime inherits from date, so this check must come first.
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(
                tzinfo=timezone.utc
            )

        return value.astimezone(
            timezone.utc
        )

    return datetime(
        year=value.year,
        month=value.month,
        day=value.day,
        tzinfo=timezone.utc,
    )


def _truncate_to_day(
    value: date | datetime,
) -> datetime:
    """Return midnight UTC for a DATE or TIMESTAMP value."""
    dt = _as_utc_datetime(value)

    if dt is None:
        raise ValueError(
            "date/datetime value is required"
        )

    return dt.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )