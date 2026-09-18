"""
Workspace/project overview + trend — Python port of the Go `store.TaskStore`
methods: `pctDelta`, `WorkspaceOverview`, `statusMeta`, `TrendRange`,
`ParseTrendRange`, `trend`, and `ProjectOverview`.

Uses asyncpg for database access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

from .stats import ProjectStats, TaskStore as StatsTaskStore


# ---------------------------------------------------------------------------
# pctDelta
# ---------------------------------------------------------------------------

def pct_delta(cur: int, prev: int) -> float:
    """
    Return the percentage change from prev to cur.

    When prev is zero it reports +100% for any growth and 0% when both
    periods are empty, avoiding a division by zero.
    """
    if prev == 0:
        return 0.0 if cur == 0 else 100.0
    return (cur - prev) / prev * 100


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

@dataclass
class ProjectSummary:
    project_id: uuid.UUID
    key: str
    name: str
    status: str
    total: int
    done: int
    in_progress: int
    todo: int
    overdue: int
    hours_logged: float
    cost_actual: float


@dataclass
class TrendPoint:
    month: str  # bucket label (day or month, per TrendRange.unit)
    created: int
    completed: int
    in_work: int
    hours: float


@dataclass
class StatusMeta:
    key: str
    label: str
    color: str


@dataclass
class WorkspaceOverview:
    by_status: dict[str, int] = field(default_factory=dict)
    by_priority: dict[str, int] = field(default_factory=dict)
    projects: list[ProjectSummary] = field(default_factory=list)
    trend: list[TrendPoint] = field(default_factory=list)
    status_meta: list[StatusMeta] = field(default_factory=list)

    total_tasks: int = 0
    overdue_tasks: int = 0
    done_tasks: int = 0
    in_progress_task: int = 0
    backlog_tasks: int = 0

    project_count: int = 0
    member_count: int = 0
    hours_logged: float = 0.0
    cost_actual: float = 0.0

    created_delta: float = 0.0
    completed_delta: float = 0.0


@dataclass
class AssigneeLoad:
    user_id: Optional[uuid.UUID]
    display_name: str
    total: int
    done: int
    overdue: int
    hours_logged: float


@dataclass
class ProjectOverview(ProjectStats):
    trend: list[TrendPoint] = field(default_factory=list)
    assignees: list[AssigneeLoad] = field(default_factory=list)
    status_meta: list[StatusMeta] = field(default_factory=list)
    overdue_tasks: int = 0
    created_delta: float = 0.0
    completed_delta: float = 0.0


# ---------------------------------------------------------------------------
# TrendRange
# ---------------------------------------------------------------------------

@dataclass
class TrendRange:
    """Selects the bucket size and how many buckets the trend covers."""

    unit: str  # "day" or "month"
    count: int  # number of buckets, ending with the current one


def parse_trend_range(s: str) -> TrendRange:
    """
    Map the API's `range` query value to a bucketing choice.

    Unknown values fall back to 30 days, which is the dashboard default.
    """
    if s == "6m":
        return TrendRange(unit="month", count=6)
    if s == "12m":
        return TrendRange(unit="month", count=12)
    return TrendRange(unit="day", count=30)  # "30d" / default


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------

class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def workspace_overview(
        self,
        workspace_id: uuid.UUID,
        tr: TrendRange,
    ) -> WorkspaceOverview:
        """Aggregate every project in a workspace for the dashboard."""
        o = WorkspaceOverview()

        # Task counts by status (+ overdue) across the workspace.
        rows = await self.pool.fetch(
            """
            SELECT t.status, count(*),
                   count(*) FILTER (WHERE t.status <> 'done' AND t.due_date IS NOT NULL AND t.due_date < CURRENT_DATE)
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE p.workspace_id = $1
            GROUP BY t.status
            """,
            workspace_id,
        )
        for status, cnt, overdue in rows:
            o.by_status[status] = cnt
            o.total_tasks += cnt
            o.overdue_tasks += overdue
            if status == "done":
                o.done_tasks += cnt
            elif status == "in_progress":
                o.in_progress_task += cnt
            elif status == "todo":
                o.backlog_tasks += cnt

        # Priority breakdown.
        prows = await self.pool.fetch(
            """
            SELECT t.priority, count(*)
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE p.workspace_id = $1
            GROUP BY t.priority
            """,
            workspace_id,
        )
        for pr, cnt in prows:
            o.by_priority[pr] = cnt

        # Headline counters + hours/cost.
        row = await self.pool.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM projects WHERE workspace_id = $1),
              (SELECT count(*) FROM workspace_members WHERE workspace_id = $1),
              (SELECT COALESCE(SUM(w.minutes),0)/60.0
                 FROM worklogs w JOIN tasks t ON t.id = w.task_id JOIN projects p ON p.id = t.project_id
                 WHERE p.workspace_id = $1),
              (SELECT COALESCE(SUM(w.minutes/60.0 * COALESCE(r.hourly_rate,0)),0)
                 FROM worklogs w JOIN tasks t ON t.id = w.task_id JOIN projects p ON p.id = t.project_id
                 LEFT JOIN user_rates r ON r.user_id = w.user_id
                 WHERE p.workspace_id = $1)
            """,
            workspace_id,
        )
        o.project_count, o.member_count, o.hours_logged, o.cost_actual = row

        # 30d vs previous 30d deltas.
        deltas_row = await self.pool.fetchrow(
            """
            WITH done_at AS (
                SELECT COALESCE((SELECT max(a.created_at) FROM activity_events a
                                  WHERE a.task_id = t.id AND a.verb='status_changed' AND a.meta->>'to'='done'),
                                t.updated_at) AS at
                FROM tasks t JOIN projects p ON p.id = t.project_id
                WHERE p.workspace_id = $1 AND t.status = 'done'
            )
            SELECT
              (SELECT count(*) FROM tasks t JOIN projects p ON p.id=t.project_id
                 WHERE p.workspace_id=$1 AND t.created_at >= now() - interval '30 days'),
              (SELECT count(*) FROM tasks t JOIN projects p ON p.id=t.project_id
                 WHERE p.workspace_id=$1 AND t.created_at >= now() - interval '60 days'
                   AND t.created_at < now() - interval '30 days'),
              (SELECT count(*) FROM done_at WHERE at >= now() - interval '30 days'),
              (SELECT count(*) FROM done_at WHERE at >= now() - interval '60 days' AND at < now() - interval '30 days')
            """,
            workspace_id,
        )
        cur_created, prev_created, cur_done, prev_done = deltas_row
        o.created_delta = pct_delta(cur_created, prev_created)
        o.completed_delta = pct_delta(cur_done, prev_done)

        # Per-project rollup.
        sum_rows = await self.pool.fetch(
            """
            SELECT p.id, p.key, p.name, p.status,
                   count(t.id),
                   count(t.id) FILTER (WHERE t.status = 'done'),
                   count(t.id) FILTER (WHERE t.status = 'in_progress'),
                   count(t.id) FILTER (WHERE t.status = 'todo'),
                   count(t.id) FILTER (WHERE t.status <> 'done' AND t.due_date IS NOT NULL AND t.due_date < CURRENT_DATE),
                   COALESCE((SELECT SUM(w.minutes)/60.0 FROM worklogs w JOIN tasks wt ON wt.id = w.task_id WHERE wt.project_id = p.id), 0),
                   COALESCE((SELECT SUM(w.minutes/60.0 * COALESCE(r.hourly_rate,0))
                             FROM worklogs w JOIN tasks wt ON wt.id = w.task_id
                             LEFT JOIN user_rates r ON r.user_id = w.user_id
                             WHERE wt.project_id = p.id), 0)
            FROM projects p
            LEFT JOIN tasks t ON t.project_id = p.id
            WHERE p.workspace_id = $1
            GROUP BY p.id, p.key, p.name, p.status, p.created_at
            ORDER BY p.created_at DESC
            """,
            workspace_id,
        )
        for r in sum_rows:
            o.projects.append(
                ProjectSummary(
                    project_id=r[0],
                    key=r[1],
                    name=r[2],
                    status=r[3],
                    total=r[4],
                    done=r[5],
                    in_progress=r[6],
                    todo=r[7],
                    overdue=r[8],
                    hours_logged=r[9],
                    cost_actual=r[10],
                )
            )

        o.trend = await self._trend(workspace_id, tr, scope="workspace")
        o.status_meta = await self._status_meta(workspace_id, scope="workspace")

        return o

    async def _status_meta(self, scoped_id: uuid.UUID, scope: str) -> list[StatusMeta]:
        """
        Return the display name and colour of every workflow column in
        scope, so charts can label and colour project-defined statuses.

        Keys are unique per project, so a workspace with several projects
        can define the same key twice; the first definition wins, which
        keeps one slice per key in the charts.
        """
        scope_col = "p.workspace_id" if scope != "project" else "p.id"

        rows = await self.pool.fetch(
            f"""
            SELECT DISTINCT ON (ws.key) ws.key, ws.name, ws.color
            FROM workflow_statuses ws
            JOIN projects p ON p.id = ws.project_id
            WHERE {scope_col} = $1
            ORDER BY ws.key, ws.position
            """,
            scoped_id,
        )
        return [StatusMeta(key=r[0], label=r[1], color=r[2]) for r in rows]

    async def _trend(
        self,
        scoped_id: uuid.UUID,
        tr: TrendRange,
        scope: str,
    ) -> list[TrendPoint]:
        """
        Return created/completed/in-work/hours activity per bucket, scoped
        either to a whole workspace or a single project.

        A task's "reached status X" timestamp comes from its most recent
        status_changed activity event, falling back to updated_at when the
        task was created directly in that status (or predates activity
        logging). Each task is therefore counted at most once per series.
        """
        # The scope predicate is chosen from a fixed set — never interpolated input.
        scope_col = "p.workspace_id" if scope != "project" else "p.id"

        # Likewise the bucket unit: whitelisted here, so it is safe to inline
        # into date_trunc (which cannot take the unit as a bind parameter
        # cleanly).
        if tr.unit == "day":
            unit, label_fmt = "day", "YYYY-MM-DD"
        else:
            unit, label_fmt = "month", "YYYY-MM"

        count = tr.count
        if count < 1 or count > 366:
            count = 30

        rows = await self.pool.fetch(
            f"""
            WITH buckets AS (
                SELECT date_trunc('{unit}', CURRENT_DATE) - (n || ' {unit}')::interval AS m
                FROM generate_series($2::int - 1, 0, -1) AS n
            ),
            scoped AS (
                SELECT t.id, t.status, t.created_at, t.updated_at
                FROM tasks t JOIN projects p ON p.id = t.project_id
                WHERE {scope_col} = $1
            ),
            reached AS (
                SELECT s.id, s.status,
                       COALESCE((SELECT max(a.created_at) FROM activity_events a
                                  WHERE a.task_id = s.id AND a.verb = 'status_changed'
                                    AND a.meta->>'to' = s.status), s.updated_at) AS at
                FROM scoped s
            )
            SELECT to_char(buckets.m, '{label_fmt}'),
                (SELECT count(*) FROM scoped s WHERE date_trunc('{unit}', s.created_at) = buckets.m),
                (SELECT count(*) FROM reached r WHERE r.status = 'done' AND date_trunc('{unit}', r.at) = buckets.m),
                (SELECT count(*) FROM reached r WHERE r.status = 'in_progress' AND date_trunc('{unit}', r.at) = buckets.m),
                (SELECT COALESCE(SUM(w.minutes),0)/60.0 FROM worklogs w JOIN tasks t ON t.id = w.task_id JOIN projects p ON p.id = t.project_id
                   WHERE {scope_col} = $1 AND date_trunc('{unit}', w.logged_on) = buckets.m)
            FROM buckets
            ORDER BY buckets.m
            """,
            scoped_id,
            count,
        )

        return [
            TrendPoint(month=r[0], created=r[1], completed=r[2], in_work=r[3], hours=r[4])
            for r in rows
        ]

    async def project_overview(
        self,
        project_id: uuid.UUID,
        tr: TrendRange,
    ) -> ProjectOverview:
        """Extend project_stats with trend and per-assignee load."""
        base = await self.project_stats(project_id)
        o = ProjectOverview(
            by_status=dict(base.by_status),
            by_priority=dict(base.by_priority),
            total=base.total,
            done=base.done,
            story_points_total=base.story_points_total,
            story_points_done=base.story_points_done,
            hours_logged=base.hours_logged,
            cost_actual=base.cost_actual,
        )

        row = await self.pool.fetchrow(
            """
            WITH done_at AS (
                SELECT COALESCE((SELECT max(a.created_at) FROM activity_events a
                                  WHERE a.task_id = t.id AND a.verb='status_changed' AND a.meta->>'to'='done'),
                                t.updated_at) AS at
                FROM tasks t
                WHERE t.project_id = $1 AND t.status = 'done'
            )
            SELECT
              (SELECT count(*) FROM tasks WHERE project_id=$1 AND created_at >= now() - interval '30 days'),
              (SELECT count(*) FROM tasks WHERE project_id=$1 AND created_at >= now() - interval '60 days'
                 AND created_at < now() - interval '30 days'),
              (SELECT count(*) FROM done_at WHERE at >= now() - interval '30 days'),
              (SELECT count(*) FROM done_at WHERE at >= now() - interval '60 days' AND at < now() - interval '30 days'),
              (SELECT count(*) FROM tasks WHERE project_id=$1 AND status <> 'done'
                 AND due_date IS NOT NULL AND due_date < CURRENT_DATE)
            """,
            project_id,
        )
        cur_created, prev_created, cur_done, prev_done, overdue_tasks = row
        o.overdue_tasks = overdue_tasks
        o.created_delta = pct_delta(cur_created, prev_created)
        o.completed_delta = pct_delta(cur_done, prev_done)

        o.trend = await self._trend(project_id, tr, scope="project")
        o.status_meta = await self._status_meta(project_id, scope="project")

        # Per-assignee load (unassigned tasks collapse into a single bucket).
        rows = await self.pool.fetch(
            """
            SELECT t.assignee_id, COALESCE(u.display_name, u.email, 'Chưa gán'),
                   count(*),
                   count(*) FILTER (WHERE t.status = 'done'),
                   count(*) FILTER (WHERE t.status <> 'done' AND t.due_date IS NOT NULL AND t.due_date < CURRENT_DATE),
                   COALESCE((SELECT SUM(w.minutes)/60.0 FROM worklogs w
                             WHERE w.task_id IN (SELECT id FROM tasks WHERE project_id = $1 AND assignee_id IS NOT DISTINCT FROM t.assignee_id)), 0)
            FROM tasks t
            LEFT JOIN users u ON u.id = t.assignee_id
            WHERE t.project_id = $1
            GROUP BY t.assignee_id, u.display_name, u.email
            ORDER BY count(*) DESC
            """,
            project_id,
        )
        for r in rows:
            o.assignees.append(
                AssigneeLoad(
                    user_id=r[0],
                    display_name=r[1],
                    total=r[2],
                    done=r[3],
                    overdue=r[4],
                    hours_logged=r[5],
                )
            )

        return o

    async def project_stats(self, project_id: uuid.UUID) -> ProjectStats:
        """
        Placeholder — the Go source references `s.ProjectStats(ctx, projectID)`
        but does not define it in the provided snippet. Implement this to
        match your actual `ProjectStats` query/shape.
        """
        raise NotImplementedError(
            "project_stats is referenced by project_overview but was not "
            "defined in the provided Go source — implement it to match "
            "TaskStore.ProjectStats."
        )