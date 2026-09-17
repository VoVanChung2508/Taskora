import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a requested row does not exist or is not accessible."""
    pass


SPRINT_COLUMNS = (
    "id, project_id, name, goal, state, start_date, end_date, "
    "position, created_at, updated_at"
)


@dataclass
class Sprint:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    goal: str
    state: str
    start_date: Optional[date]
    end_date: Optional[date]
    position: int
    created_at: datetime
    updated_at: datetime


def scan_sprint(row: Optional[asyncpg.Record]) -> Sprint:
    if row is None:
        raise NotFoundError
    return Sprint(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        goal=row["goal"],
        state=row["state"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        position=row["position"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@dataclass
class SprintUpdateFields:
    """Carries optional sprint edits. The set_* flags allow clearing the
    nullable date columns."""

    name: Optional[str] = None
    goal: Optional[str] = None
    state: Optional[str] = None

    set_start_date: bool = False
    start_date: Optional[date] = None

    set_end_date: bool = False
    end_date: Optional[date] = None


class SprintStore:
    """Handles persistence for sprints."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, project_id: uuid.UUID, name: str, goal: str) -> Sprint:
        """Inserts a sprint at the end of the project's sprint order."""
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO sprints (project_id, name, goal, position)
            VALUES (
                $1, $2, $3,
                COALESCE((SELECT MAX(position)+1 FROM sprints WHERE project_id=$1), 0)
            )
            RETURNING {SPRINT_COLUMNS}
            """,
            project_id,
            name,
            goal,
        )
        return scan_sprint(row)

    async def list_by_project(self, project_id: uuid.UUID) -> list[Sprint]:
        """Returns a project's sprints ordered by position."""
        rows = await self.pool.fetch(
            f"SELECT {SPRINT_COLUMNS} FROM sprints WHERE project_id=$1 ORDER BY position",
            project_id,
        )
        return [scan_sprint(row) for row in rows]

    async def get_by_id(self, id: uuid.UUID) -> Sprint:
        """Fetches a sprint by id."""
        row = await self.pool.fetchrow(
            f"SELECT {SPRINT_COLUMNS} FROM sprints WHERE id=$1", id
        )
        return scan_sprint(row)

    async def update(self, id: uuid.UUID, f: SprintUpdateFields) -> Sprint:
        """Patches a sprint's editable fields."""
        row = await self.pool.fetchrow(
            f"""
            UPDATE sprints SET
                name = COALESCE($2, name),
                goal = COALESCE($3, goal),
                state = COALESCE($4::sprint_state, state),
                start_date = CASE WHEN $5 THEN $6 ELSE start_date END,
                end_date = CASE WHEN $7 THEN $8 ELSE end_date END
            WHERE id = $1
            RETURNING {SPRINT_COLUMNS}
            """,
            id,
            f.name,
            f.goal,
            f.state,
            f.set_start_date,
            f.start_date,
            f.set_end_date,
            f.end_date,
        )
        return scan_sprint(row)