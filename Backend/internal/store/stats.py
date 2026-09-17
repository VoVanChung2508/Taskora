import uuid
from dataclasses import dataclass, field

import asyncpg


@dataclass
class ProjectStats:
    """Aggregates a project's task metrics for analytics."""

    by_status: dict[str, int] = field(default_factory=dict)
    by_priority: dict[str, int] = field(default_factory=dict)
    total: int = 0
    done: int = 0
    story_points_total: float = 0.0
    story_points_done: float = 0.0
    hours_logged: float = 0.0
    cost_actual: float = 0.0


@dataclass
class DashboardStats:
    """Aggregates a user's cross-workspace metrics."""

    workspace_count: int = 0
    project_count: int = 0
    open_tasks: int = 0
    due_soon: int = 0
    hours_this_week: float = 0.0


class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def project_stats(self, project_id: uuid.UUID) -> ProjectStats:
        """Aggregates a project's task metrics for analytics."""
        stats = ProjectStats()

        # Counts + story points by status.
        rows = await self.pool.fetch(
            """
            SELECT status, count(*), COALESCE(SUM(story_points),0)
            FROM tasks WHERE project_id=$1 GROUP BY status
            """,
            project_id,
        )
        for row in rows:
            status, cnt, pts = row["status"], row["count"], row["coalesce"]
            stats.by_status[status] = cnt
            stats.total += cnt
            stats.story_points_total += pts
            if status == "done":
                stats.done += cnt
                stats.story_points_done += pts

        # Counts by priority.
        prows = await self.pool.fetch(
            """
            SELECT priority, count(*) FROM tasks WHERE project_id=$1 GROUP BY priority
            """,
            project_id,
        )
        for row in prows:
            stats.by_priority[row["priority"]] = row["count"]

        # Hours logged + actual cost (worklog minutes × hourly rate).
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(SUM(w.minutes),0)/60.0,
                   COALESCE(SUM(w.minutes/60.0 * COALESCE(r.hourly_rate,0)),0)
            FROM worklogs w
            JOIN tasks t ON t.id = w.task_id
            LEFT JOIN user_rates r ON r.user_id = w.user_id
            WHERE t.project_id=$1
            """,
            project_id,
        )
        stats.hours_logged = row[0]
        stats.cost_actual = row[1]

        return stats

    async def dashboard_stats(self, user_id: uuid.UUID) -> DashboardStats:
        """Aggregates a user's cross-workspace metrics."""
        row = await self.pool.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM workspace_members WHERE user_id=$1),
              (SELECT count(*) FROM projects p JOIN workspace_members m ON m.workspace_id=p.workspace_id WHERE m.user_id=$1),
              (SELECT count(*) FROM tasks t JOIN projects p ON p.id=t.project_id JOIN workspace_members m ON m.workspace_id=p.workspace_id
                 WHERE m.user_id=$1 AND t.status <> 'done'),
              (SELECT count(*) FROM tasks t JOIN projects p ON p.id=t.project_id JOIN workspace_members m ON m.workspace_id=p.workspace_id
                 WHERE m.user_id=$1 AND t.status <> 'done' AND t.due_date IS NOT NULL AND t.due_date <= CURRENT_DATE + 7),
              (SELECT COALESCE(SUM(minutes),0)/60.0 FROM worklogs WHERE user_id=$1 AND logged_on >= date_trunc('week', CURRENT_DATE))
            """,
            user_id,
        )
        return DashboardStats(
            workspace_count=row[0],
            project_count=row[1],
            open_tasks=row[2],
            due_soon=row[3],
            hours_this_week=row[4],
        )

    async def workspace_dashboard_stats(
        self, user_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> DashboardStats:
        """Aggregates a user's metrics for a specific workspace."""
        row = await self.pool.fetchrow(
            """
            SELECT
              1,
              (SELECT count(*) FROM projects p JOIN workspace_members m ON m.workspace_id=p.workspace_id WHERE m.user_id=$1 AND p.workspace_id=$2),
              (SELECT count(*) FROM tasks t JOIN projects p ON p.id=t.project_id JOIN workspace_members m ON m.workspace_id=p.workspace_id
                 WHERE m.user_id=$1 AND p.workspace_id=$2 AND t.status <> 'done'),
              (SELECT count(*) FROM tasks t JOIN projects p ON p.id=t.project_id JOIN workspace_members m ON m.workspace_id=p.workspace_id
                 WHERE m.user_id=$1 AND p.workspace_id=$2 AND t.status <> 'done' AND t.due_date IS NOT NULL AND t.due_date <= CURRENT_DATE + 7),
              (SELECT COALESCE(SUM(w.minutes),0)/60.0 FROM worklogs w JOIN tasks t ON t.id=w.task_id JOIN projects p ON p.id=t.project_id WHERE w.user_id=$1 AND p.workspace_id=$2 AND w.logged_on >= date_trunc('week', CURRENT_DATE))
            """,
            user_id,
            workspace_id,
        )
        return DashboardStats(
            workspace_count=row[0],
            project_count=row[1],
            open_tasks=row[2],
            due_soon=row[3],
            hours_this_week=row[4],
        )