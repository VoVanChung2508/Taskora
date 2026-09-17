import uuid
from dataclasses import dataclass
from typing import NamedTuple, Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


class LastStatusError(Exception):
    """Raised when deleting the only remaining board column."""
    pass


class DefaultStatus(NamedTuple):
    key: str
    name: str
    category: str
    color: str


# The column set seeded for every new project. It mirrors what the UI
# shipped with before statuses became configurable.
DEFAULT_STATUSES: list[DefaultStatus] = [
    DefaultStatus("todo", "To Do", "todo", "blue"),
    DefaultStatus("in_progress", "In Work", "in_progress", "purple"),
    DefaultStatus("in_review", "On Review", "in_progress", "orange"),
    DefaultStatus("done", "Done", "done", "green"),
]


@dataclass
class WorkflowStatus:
    id: uuid.UUID
    project_id: uuid.UUID
    key: str
    name: str
    category: str
    color: str
    position: float
    wip_limit: Optional[int]
    task_count: int = 0


@dataclass
class StatusUpdateFields:
    """Carries optional column edits."""

    name: Optional[str] = None
    category: Optional[str] = None
    color: Optional[str] = None
    position: Optional[float] = None

    set_wip_limit: bool = False
    wip_limit: Optional[int] = None


class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def seed_default_statuses(self, project_id: uuid.UUID) -> None:
        """Creates the built-in columns for a newly created project."""
        for i, d in enumerate(DEFAULT_STATUSES):
            await self.pool.execute(
                """
                INSERT INTO workflow_statuses (project_id, key, name, category, color, position)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (project_id, key) DO NOTHING
                """,
                project_id,
                d.key,
                d.name,
                d.category,
                d.color,
                float(i),
            )

    async def list_statuses(self, project_id: uuid.UUID) -> list[WorkflowStatus]:
        """Returns a project's board columns with live task counts."""
        rows = await self.pool.fetch(
            """
            SELECT w.id, w.project_id, w.key, w.name, w.category, w.color, w.position, w.wip_limit,
                   (SELECT count(*) FROM tasks t
                     WHERE t.project_id = w.project_id AND t.status = w.key
                       AND t.parent_task_id IS NULL)
            FROM workflow_statuses w
            WHERE w.project_id = $1
            ORDER BY w.position
            """,
            project_id,
        )
        out = []
        for row in rows:
            out.append(
                WorkflowStatus(
                    id=row[0],
                    project_id=row[1],
                    key=row[2],
                    name=row[3],
                    category=row[4],
                    color=row[5],
                    position=row[6],
                    wip_limit=row[7],
                    task_count=row[8],
                )
            )
        return out

    async def create_status(
        self,
        project_id: uuid.UUID,
        key: str,
        name: str,
        category: str,
        color: str,
        wip_limit: Optional[int],
    ) -> WorkflowStatus:
        """Appends a column to a project's board."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO workflow_statuses (project_id, key, name, category, color, position, wip_limit)
            VALUES ($1, $2, $3, $4, $5,
                    COALESCE((SELECT MAX(position)+1 FROM workflow_statuses WHERE project_id=$1), 0),
                    $6)
            RETURNING id, project_id, key, name, category, color, position, wip_limit
            """,
            project_id,
            key,
            name,
            category,
            color,
            wip_limit,
        )
        return WorkflowStatus(
            id=row["id"],
            project_id=row["project_id"],
            key=row["key"],
            name=row["name"],
            category=row["category"],
            color=row["color"],
            position=row["position"],
            wip_limit=row["wip_limit"],
        )

    async def update_workflow_status(
        self, project_id: uuid.UUID, status_id: uuid.UUID, f: StatusUpdateFields
    ) -> None:
        """Patches a board column."""
        result = await self.pool.execute(
            """
            UPDATE workflow_statuses SET
                name = COALESCE($3, name),
                category = COALESCE($4, category),
                color = COALESCE($5, color),
                position = COALESCE($6, position),
                wip_limit = CASE WHEN $7 THEN $8 ELSE wip_limit END
            WHERE id = $2 AND project_id = $1
            """,
            project_id,
            status_id,
            f.name,
            f.category,
            f.color,
            f.position,
            f.set_wip_limit,
            f.wip_limit,
        )
        if result.split()[-1] == "0":
            raise NotFoundError

    async def count_tasks_in_status(self, project_id: uuid.UUID, status_key: str) -> int:
        """Reports how many top-level tasks sit in a column, used to
        enforce WIP limits."""
        return await self.pool.fetchval(
            """
            SELECT count(*) FROM tasks
            WHERE project_id = $1 AND status = $2 AND parent_task_id IS NULL
            """,
            project_id,
            status_key,
        )

    async def wip_limit_for(
        self, project_id: uuid.UUID, status_key: str
    ) -> Optional[int]:
        """Returns the configured WIP limit for a column, or None when the
        project has no such column or no limit set."""
        row = await self.pool.fetchrow(
            "SELECT wip_limit FROM workflow_statuses WHERE project_id = $1 AND key = $2",
            project_id,
            status_key,
        )
        if row is None:
            return None  # no column defined → no limit
        return row["wip_limit"]

    async def delete_status(self, project_id: uuid.UUID, status_id: uuid.UUID) -> None:
        """Removes a column. Tasks still in it are moved to the fallback
        column (the first remaining one by position) so no task is
        orphaned."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                key = await conn.fetchval(
                    "SELECT key FROM workflow_statuses WHERE id = $1 AND project_id = $2",
                    status_id,
                    project_id,
                )
                if key is None:
                    raise NotFoundError

                fallback = await conn.fetchval(
                    """
                    SELECT key FROM workflow_statuses
                    WHERE project_id = $1 AND id <> $2
                    ORDER BY position LIMIT 1
                    """,
                    project_id,
                    status_id,
                )
                if fallback is None:
                    # Refuse to delete the last column — the board would
                    # have nowhere to put tasks.
                    raise LastStatusError

                await conn.execute(
                    "UPDATE tasks SET status = $3 WHERE project_id = $1 AND status = $2",
                    project_id,
                    key,
                    fallback,
                )
                await conn.execute(
                    "DELETE FROM workflow_statuses WHERE id = $1 AND project_id = $2",
                    status_id,
                    project_id,
                )