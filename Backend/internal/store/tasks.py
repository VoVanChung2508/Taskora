import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


TASK_COLUMNS = (
    "id, project_id, number, parent_task_id, title, description, status, priority, "
    "assignee_id, reporter_id, participant_ids, story_points, start_date, due_date, "
    "position, sprint_id, start_at, end_at, moscow, rice_reach, rice_impact, "
    "rice_confidence, rice_effort, rice_score, created_at, updated_at"
)


def prefix_cols(alias: str, columns: str) -> str:
    """Prefixes each comma-separated column name with a table alias, e.g.
    prefix_cols("t", "id, name") -> "t.id, t.name"."""
    return ", ".join(f"{alias}.{c.strip()}" for c in columns.split(","))


@dataclass
class Task:
    id: uuid.UUID
    project_id: uuid.UUID
    number: int
    parent_task_id: Optional[uuid.UUID]
    title: str
    description: str
    status: str
    priority: str
    assignee_id: Optional[uuid.UUID]
    reporter_id: Optional[uuid.UUID]
    participant_ids: list[uuid.UUID]
    story_points: Optional[float]
    start_date: Optional[date]
    due_date: Optional[date]
    position: int
    sprint_id: Optional[uuid.UUID]
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    moscow: Optional[str]
    rice_reach: Optional[float]
    rice_impact: Optional[float]
    rice_confidence: Optional[float]
    rice_effort: Optional[float]
    rice_score: Optional[float]
    created_at: datetime
    updated_at: datetime


def scan_task(row: Optional[asyncpg.Record]) -> Task:
    if row is None:
        raise NotFoundError
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        number=row["number"],
        parent_task_id=row["parent_task_id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        priority=row["priority"],
        assignee_id=row["assignee_id"],
        reporter_id=row["reporter_id"],
        participant_ids=row["participant_ids"],
        story_points=row["story_points"],
        start_date=row["start_date"],
        due_date=row["due_date"],
        position=row["position"],
        sprint_id=row["sprint_id"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        moscow=row["moscow"],
        rice_reach=row["rice_reach"],
        rice_impact=row["rice_impact"],
        rice_confidence=row["rice_confidence"],
        rice_effort=row["rice_effort"],
        rice_score=row["rice_score"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@dataclass
class CreateTaskParams:
    """Carries inputs to create a task."""

    project_id: uuid.UUID
    title: str
    reporter_id: uuid.UUID
    parent_task_id: Optional[uuid.UUID] = None
    description: str = ""
    status: str = ""
    priority: str = ""
    assignee_id: Optional[uuid.UUID] = None
    participant_ids: Optional[list[uuid.UUID]] = None


@dataclass
class TaskUpdateFields:
    """Carries optional task edits. A non-None value = "set"; the set_*
    booleans allow explicitly clearing nullable fields to NULL."""

    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None

    set_assignee: bool = False
    assignee_id: Optional[uuid.UUID] = None

    set_participants: bool = False
    participant_ids: Optional[list[uuid.UUID]] = None

    set_story_points: bool = False
    story_points: Optional[float] = None

    set_start_date: bool = False
    start_date: Optional[date] = None

    set_due_date: bool = False
    due_date: Optional[date] = None

    set_start_at: bool = False
    start_at: Optional[datetime] = None

    set_end_at: bool = False
    end_at: Optional[datetime] = None

    set_reporter: bool = False
    reporter_id: Optional[uuid.UUID] = None

    # Backlog prioritisation. RICE inputs are set as one group so a partial
    # update never leaves a half-filled score.
    set_moscow: bool = False
    moscow: Optional[str] = None

    set_rice: bool = False
    rice_reach: Optional[float] = None
    rice_impact: Optional[float] = None
    rice_confidence: Optional[float] = None
    rice_effort: Optional[float] = None


@dataclass
class TaskListItem:
    task: Task
    labels: list["Label"] = field(default_factory=list)
    comment_count: int = 0
    checklist_total: int = 0
    checklist_done: int = 0
    subtask_count: int = 0


@dataclass
class CalendarItem:
    id: uuid.UUID
    title: str
    status: str
    priority: str
    start_date: Optional[date]
    due_date: Optional[date]
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    assignee_id: Optional[uuid.UUID]
    participant_ids: list[uuid.UUID]
    assignee_name: Optional[str]
    assignee_avatar: Optional[str]
    project_id: uuid.UUID
    project_key: str
    project_name: str


class TaskStore:
    """Handles persistence for tasks."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, p: CreateTaskParams) -> Task:
        """Inserts a task, placing it at the end of its status column."""
        status = p.status or "todo"
        priority = p.priority or "medium"
        participant_ids = p.participant_ids if p.participant_ids is not None else []

        position = await self.pool.fetchval(
            "SELECT COALESCE(MAX(position), 0) + 65536 FROM tasks WHERE project_id = $1 AND status = $2",
            p.project_id,
            status,
        )

        # reporter_id is a nullable FK: a nil UUID means "no human reporter"
        # (e.g. a task created through the public API) and must be stored
        # as NULL.
        reporter = p.reporter_id if p.reporter_id != uuid.UUID(int=0) else None

        row = await self.pool.fetchrow(
            f"""
            INSERT INTO tasks (project_id, parent_task_id, title, description, status, priority, assignee_id, reporter_id, participant_ids, position)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING {TASK_COLUMNS}
            """,
            p.project_id,
            p.parent_task_id,
            p.title,
            p.description,
            status,
            priority,
            p.assignee_id,
            reporter,
            participant_ids,
            position,
        )
        return scan_task(row)

    async def get_by_id(self, id: uuid.UUID) -> Task:
        """Fetches a task by id."""
        row = await self.pool.fetchrow(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = $1", id
        )
        return scan_task(row)

    async def delete(self, id: uuid.UUID) -> None:
        """Removes a task by id."""
        result = await self.pool.execute("DELETE FROM tasks WHERE id = $1", id)
        if result.split()[-1] == "0":
            raise NotFoundError

    async def list_by_project(self, project_id: uuid.UUID) -> list[Task]:
        """Returns tasks in a project ordered by status then position."""
        rows = await self.pool.fetch(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE project_id = $1 ORDER BY status, position",
            project_id,
        )
        return [scan_task(row) for row in rows]

    async def list_by_project_enriched(self, project_id: uuid.UUID) -> list[TaskListItem]:
        """Returns tasks with labels + aggregate counts for board/list
        rendering."""
        rows = await self.pool.fetch(
            f"""
            SELECT {prefix_cols("t", TASK_COLUMNS)},
                (SELECT count(*) FROM comments c WHERE c.task_id = t.id) AS comment_count,
                (SELECT count(*) FROM checklist_items ci WHERE ci.task_id = t.id) AS checklist_total,
                (SELECT count(*) FROM checklist_items ci WHERE ci.task_id = t.id AND ci.done) AS checklist_done,
                (SELECT count(*) FROM tasks st WHERE st.parent_task_id = t.id) AS subtask_count
            FROM tasks t
            WHERE t.project_id = $1 AND t.parent_task_id IS NULL
            ORDER BY t.status, t.position
            """,
            project_id,
        )

        items: dict[uuid.UUID, TaskListItem] = {}
        order: list[uuid.UUID] = []
        for row in rows:
            task = scan_task(row)
            li = TaskListItem(
                task=task,
                labels=[],
                comment_count=row["comment_count"],
                checklist_total=row["checklist_total"],
                checklist_done=row["checklist_done"],
                subtask_count=row["subtask_count"],
            )
            items[task.id] = li
            order.append(task.id)

        # Attach labels in one query.
        lrows = await self.pool.fetch(
            """
            SELECT tl.task_id, l.id, l.project_id, l.name, l.color
            FROM task_labels tl
            JOIN labels l ON l.id = tl.label_id
            JOIN tasks t ON t.id = tl.task_id
            WHERE t.project_id = $1
            """,
            project_id,
        )
        for lrow in lrows:
            task_id = lrow["task_id"]
            label = Label(
                id=lrow["id"],
                project_id=lrow["project_id"],
                name=lrow["name"],
                color=lrow["color"],
            )
            if task_id in items:
                items[task_id].labels.append(label)

        return [items[tid] for tid in order]

    async def calendar_for_user(
        self, user_id: uuid.UUID, from_: datetime, to: datetime
    ) -> list[CalendarItem]:
        """Returns tasks (with a due date in range) across all projects in
        the workspaces the user belongs to."""
        rows = await self.pool.fetch(
            """
            SELECT t.id, t.title, t.status, t.priority, t.start_date, t.due_date, t.start_at, t.end_at, t.assignee_id, t.participant_ids,
                   u.display_name, u.avatar_url,
                   p.id, p.key, p.name
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            JOIN workspace_members m ON m.workspace_id = p.workspace_id AND m.user_id = $1
            LEFT JOIN users u ON u.id = t.assignee_id
            WHERE (t.start_at >= $2 AND t.start_at < $3)
               OR (t.start_at IS NULL AND t.due_date >= $2::date AND t.due_date <= $3::date)
            ORDER BY t.start_at NULLS LAST, t.due_date
            """,
            user_id,
            from_,
            to,
        )
        out = []
        for row in rows:
            participant_ids = row[9] if row[9] is not None else []
            out.append(
                CalendarItem(
                    id=row[0],
                    title=row[1],
                    status=row[2],
                    priority=row[3],
                    start_date=row[4],
                    due_date=row[5],
                    start_at=row[6],
                    end_at=row[7],
                    assignee_id=row[8],
                    participant_ids=participant_ids,
                    assignee_name=row[10],
                    assignee_avatar=row[11],
                    project_id=row[12],
                    project_key=row[13],
                    project_name=row[14],
                )
            )
        return out

    async def update(self, id: uuid.UUID, f: TaskUpdateFields) -> Task:
        """Applies partial updates to a task's editable fields."""
        row = await self.pool.fetchrow(
            f"""
            UPDATE tasks SET
                title = COALESCE($2, title),
                description = COALESCE($3, description),
                priority = COALESCE($4, priority),
                assignee_id = CASE WHEN $5 THEN $6 ELSE assignee_id END,
                participant_ids = CASE WHEN $7 THEN $8 ELSE participant_ids END,
                story_points = CASE WHEN $9 THEN $10 ELSE story_points END,
                start_date = CASE WHEN $11 THEN $12 ELSE start_date END,
                due_date = CASE WHEN $13 THEN $14 ELSE due_date END,
                start_at = CASE WHEN $15 THEN $16 ELSE start_at END,
                end_at = CASE WHEN $17 THEN $18 ELSE end_at END,
                reporter_id = CASE WHEN $19 THEN $20 ELSE reporter_id END,
                moscow = CASE WHEN $21 THEN $22 ELSE moscow END,
                rice_reach = CASE WHEN $23 THEN $24 ELSE rice_reach END,
                rice_impact = CASE WHEN $23 THEN $25 ELSE rice_impact END,
                rice_confidence = CASE WHEN $23 THEN $26 ELSE rice_confidence END,
                rice_effort = CASE WHEN $23 THEN $27 ELSE rice_effort END
            WHERE id = $1
            RETURNING {TASK_COLUMNS}
            """,
            id,
            f.title,
            f.description,
            f.priority,
            f.set_assignee,
            f.assignee_id,
            f.set_participants,
            f.participant_ids,
            f.set_story_points,
            f.story_points,
            f.set_start_date,
            f.start_date,
            f.set_due_date,
            f.due_date,
            f.set_start_at,
            f.start_at,
            f.set_end_at,
            f.end_at,
            f.set_reporter,
            f.reporter_id,
            f.set_moscow,
            f.moscow,
            f.set_rice,
            f.rice_reach,
            f.rice_impact,
            f.rice_confidence,
            f.rice_effort,
        )
        return scan_task(row)

    async def update_status(self, id: uuid.UUID, status: str) -> Task:
        """Moves a task to a new status column (used by Kanban drag/drop)."""
        row = await self.pool.fetchrow(
            f"""
            UPDATE tasks SET status = $2,
                position = COALESCE((SELECT MAX(position) + 1 FROM tasks t2 WHERE t2.project_id = tasks.project_id AND t2.status = $2), 0)
            WHERE id = $1
            RETURNING {TASK_COLUMNS}
            """,
            id,
            status,
        )
        return scan_task(row)