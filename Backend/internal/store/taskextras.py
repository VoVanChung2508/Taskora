import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import asyncpg


@dataclass
class Label:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    color: str


@dataclass
class Comment:
    id: uuid.UUID
    task_id: uuid.UUID
    author_id: Optional[uuid.UUID]
    author_name: str
    author_email: str
    body: str
    created_at: datetime


@dataclass
class ChecklistItem:
    id: uuid.UUID
    task_id: uuid.UUID
    title: str
    done: bool
    position: int
    created_at: datetime


@dataclass
class ActivityEvent:
    id: uuid.UUID
    task_id: uuid.UUID
    actor_id: Optional[uuid.UUID]
    actor_name: str
    verb: str
    meta: Any
    created_at: datetime


class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ── Labels ───────────────────────────────────────────────────

    async def list_labels(self, project_id: uuid.UUID) -> list[Label]:
        """Returns all labels defined in a project."""
        rows = await self.pool.fetch(
            "SELECT id, project_id, name, color FROM labels WHERE project_id = $1 ORDER BY name",
            project_id,
        )
        return [
            Label(id=r["id"], project_id=r["project_id"], name=r["name"], color=r["color"])
            for r in rows
        ]

    async def create_label(self, project_id: uuid.UUID, name: str, color: str) -> Label:
        """Adds a label to a project."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO labels (project_id, name, color) VALUES ($1,$2,$3)
            RETURNING id, project_id, name, color
            """,
            project_id,
            name,
            color,
        )
        return Label(
            id=row["id"], project_id=row["project_id"], name=row["name"], color=row["color"]
        )

    async def set_task_label(self, task_id: uuid.UUID, label_id: uuid.UUID, on: bool) -> None:
        """Adds/removes a label on a task."""
        if on:
            await self.pool.execute(
                "INSERT INTO task_labels (task_id, label_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                task_id,
                label_id,
            )
        else:
            await self.pool.execute(
                "DELETE FROM task_labels WHERE task_id=$1 AND label_id=$2", task_id, label_id
            )

    # ── Comments ─────────────────────────────────────────────────

    async def add_comment(self, task_id: uuid.UUID, author_id: uuid.UUID, body: str) -> Comment:
        """Inserts a comment and returns it with author info."""
        row = await self.pool.fetchrow(
            """
            WITH ins AS (
                INSERT INTO comments (task_id, author_id, body) VALUES ($1,$2,$3)
                RETURNING id, task_id, author_id, body, created_at
            )
            SELECT ins.id, ins.task_id, ins.author_id, COALESCE(u.display_name,''), COALESCE(u.email::text,''), ins.body, ins.created_at
            FROM ins LEFT JOIN users u ON u.id = ins.author_id
            """,
            task_id,
            author_id,
            body,
        )
        return Comment(
            id=row[0],
            task_id=row[1],
            author_id=row[2],
            author_name=row[3],
            author_email=row[4],
            body=row[5],
            created_at=row[6],
        )

    async def list_comments(self, task_id: uuid.UUID) -> list[Comment]:
        """Returns a task's comments oldest-first."""
        rows = await self.pool.fetch(
            """
            SELECT c.id, c.task_id, c.author_id, COALESCE(u.display_name,''), COALESCE(u.email::text,''), c.body, c.created_at
            FROM comments c LEFT JOIN users u ON u.id = c.author_id
            WHERE c.task_id = $1 ORDER BY c.created_at
            """,
            task_id,
        )
        return [
            Comment(
                id=r[0],
                task_id=r[1],
                author_id=r[2],
                author_name=r[3],
                author_email=r[4],
                body=r[5],
                created_at=r[6],
            )
            for r in rows
        ]

    # ── Checklist ────────────────────────────────────────────────

    async def list_checklist(self, task_id: uuid.UUID) -> list[ChecklistItem]:
        """Returns a task's checklist items ordered by position."""
        rows = await self.pool.fetch(
            """
            SELECT id, task_id, title, done, position, created_at FROM checklist_items
            WHERE task_id=$1 ORDER BY position
            """,
            task_id,
        )
        return [
            ChecklistItem(
                id=r["id"],
                task_id=r["task_id"],
                title=r["title"],
                done=r["done"],
                position=r["position"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def add_checklist_item(self, task_id: uuid.UUID, title: str) -> ChecklistItem:
        """Appends an item to a task's checklist."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO checklist_items (task_id, title, position)
            VALUES ($1, $2, COALESCE((SELECT MAX(position)+1 FROM checklist_items WHERE task_id=$1), 0))
            RETURNING id, task_id, title, done, position, created_at
            """,
            task_id,
            title,
        )
        return ChecklistItem(
            id=row["id"],
            task_id=row["task_id"],
            title=row["title"],
            done=row["done"],
            position=row["position"],
            created_at=row["created_at"],
        )

    async def toggle_checklist_item(self, item_id: uuid.UUID, done: bool) -> None:
        """Flips an item's done state."""
        await self.pool.execute(
            "UPDATE checklist_items SET done=$2 WHERE id=$1", item_id, done
        )

    async def set_sprint(self, task_id: uuid.UUID, sprint_id: Optional[uuid.UUID]) -> None:
        """Assigns a task to a sprint (None = move to backlog)."""
        await self.pool.execute(
            "UPDATE tasks SET sprint_id=$2 WHERE id=$1", task_id, sprint_id
        )

    # ── Activity ─────────────────────────────────────────────────

    async def record_activity(
        self, task_id: uuid.UUID, actor_id: uuid.UUID, verb: str, meta: dict[str, Any]
    ) -> None:
        """Appends an audit event for a task."""
        raw = json.dumps(meta)
        await self.pool.execute(
            "INSERT INTO activity_events (task_id, actor_id, verb, meta) VALUES ($1,$2,$3,$4)",
            task_id,
            actor_id,
            verb,
            raw,
        )

    async def list_activity(self, task_id: uuid.UUID) -> list[ActivityEvent]:
        """Returns a task's activity newest-first."""
        rows = await self.pool.fetch(
            """
            SELECT a.id, a.task_id, a.actor_id, COALESCE(u.display_name,''), a.verb, a.meta, a.created_at
            FROM activity_events a LEFT JOIN users u ON u.id = a.actor_id
            WHERE a.task_id=$1 ORDER BY a.created_at DESC
            """,
            task_id,
        )
        out = []
        for r in rows:
            meta = r["meta"]
            # meta may come back as a JSON string depending on column type/codec
            if isinstance(meta, str):
                meta = json.loads(meta) if meta else None
            out.append(
                ActivityEvent(
                    id=r[0],
                    task_id=r[1],
                    actor_id=r[2],
                    actor_name=r[3],
                    verb=r[4],
                    meta=meta,
                    created_at=r[6],
                )
            )
        return out