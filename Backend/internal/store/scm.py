import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a requested row does not exist or is not accessible."""
    pass


@dataclass
class Comment:
    id: uuid.UUID
    task_id: uuid.UUID
    author_id: Optional[uuid.UUID]
    body: str
    created_at: datetime
    author_name: Optional[str] = None


class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def by_project_number(
        self, project_id: uuid.UUID, number: str
    ) -> "domain.Task":
        """Finds a task by its human-readable number within a project
        (the "12" in "SAP-12")."""
        try:
            n = int(number)
        except ValueError:
            raise NotFoundError

        row = await self.pool.fetchrow(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE project_id = $1 AND number = $2",
            project_id,
            n,
        )
        if row is None:
            raise NotFoundError
        return scan_task(row)

    async def add_system_comment(self, task_id: uuid.UUID, body: str) -> Comment:
        """Posts a comment with no author, used for machine-generated
        notes such as linked commits and pull requests."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO comments (task_id, author_id, body)
            VALUES ($1, NULL, $2)
            RETURNING id, task_id, author_id, body, created_at
            """,
            task_id,
            body,
        )

        c = Comment(
            id=row["id"],
            task_id=row["task_id"],
            author_id=row["author_id"],
            body=row["body"],
            created_at=row["created_at"],
        )
        c.author_name = "Flowie Bot"
        return c