import uuid
from dataclasses import dataclass, field

import asyncpg


class DependencyCycleError(Exception):
    """Raised when adding a dependency would create a cycle."""
    pass


class SelfDependencyError(Exception):
    """Raised when a task is made to depend on itself."""
    pass


@dataclass
class TaskDependencyItem:
    id: uuid.UUID
    title: str
    status: str
    priority: str
    project_key: str


@dataclass
class TaskDependencies:
    blocked_by: list[TaskDependencyItem] = field(default_factory=list)
    blocks: list[TaskDependencyItem] = field(default_factory=list)


class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def add_dependency(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        """Records that task_id depends on (is blocked by) depends_on_id.
        Rejects self-references and any edge that would close a cycle."""
        if task_id == depends_on_id:
            raise SelfDependencyError

        # A cycle forms if depends_on_id already (transitively) depends on
        # task_id: adding task_id → depends_on_id would then be reachable
        # back to task_id.
        cycle = await self.pool.fetchval(
            """
            WITH RECURSIVE reach AS (
                SELECT depends_on_id FROM task_dependencies WHERE task_id = $1
                UNION
                SELECT td.depends_on_id
                FROM task_dependencies td
                JOIN reach r ON td.task_id = r.depends_on_id
            )
            SELECT EXISTS(SELECT 1 FROM reach WHERE depends_on_id = $2)
            """,
            depends_on_id,
            task_id,
        )
        if cycle:
            raise DependencyCycleError

        await self.pool.execute(
            """
            INSERT INTO task_dependencies (task_id, depends_on_id, type)
            VALUES ($1, $2, 'blocks')
            ON CONFLICT (task_id, depends_on_id) DO NOTHING
            """,
            task_id,
            depends_on_id,
        )

    async def remove_dependency(self, task_id: uuid.UUID, depends_on_id: uuid.UUID) -> None:
        """Deletes the edge task_id → depends_on_id."""
        await self.pool.execute(
            "DELETE FROM task_dependencies WHERE task_id = $1 AND depends_on_id = $2",
            task_id,
            depends_on_id,
        )

    async def list_dependencies(self, task_id: uuid.UUID) -> TaskDependencies:
        """Returns the tasks that block task_id (blocked_by) and the tasks
        that task_id blocks (blocks), each enriched with title/status/key."""
        out = TaskDependencies()

        out.blocked_by = await self._query_dependency_items(
            """
            SELECT t.id, t.title, t.status, t.priority, p.key
            FROM task_dependencies td
            JOIN tasks t ON t.id = td.depends_on_id
            JOIN projects p ON p.id = t.project_id
            WHERE td.task_id = $1
            ORDER BY t.title
            """,
            task_id,
        )

        out.blocks = await self._query_dependency_items(
            """
            SELECT t.id, t.title, t.status, t.priority, p.key
            FROM task_dependencies td
            JOIN tasks t ON t.id = td.task_id
            JOIN projects p ON p.id = t.project_id
            WHERE td.depends_on_id = $1
            ORDER BY t.title
            """,
            task_id,
        )

        return out

    async def _query_dependency_items(
        self, sql: str, task_id: uuid.UUID
    ) -> list[TaskDependencyItem]:
        rows = await self.pool.fetch(sql, task_id)
        return [
            TaskDependencyItem(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                priority=row["priority"],
                project_key=row["key"],
            )
            for row in rows
        ]