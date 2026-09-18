from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .handlers import Handlers


def task_link(project_id: uuid.UUID, task_id: uuid.UUID) -> str:
    return f"/projects/{project_id}?task={task_id}"


def comment_link(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    comment_id: uuid.UUID,
) -> str:
    return f"/projects/{project_id}?task={task_id}&comment={comment_id}"


def _fmt_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d")


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _fmt_float(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_uuid(value: uuid.UUID | None) -> str:
    if value is None or value.int == 0:
        return ""
    return str(value)


def _deref_str(value: str | None) -> str:
    return value or ""


def _same_uuid(a: uuid.UUID | None, b: uuid.UUID | None) -> bool:
    return _fmt_uuid(a) == _fmt_uuid(b)


def diff_participants(
    before: Iterable[uuid.UUID],
    after: Iterable[uuid.UUID],
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    before_set = set(before or [])
    after_set = set(after or [])
    added = [item for item in (after or []) if item not in before_set]
    removed = [item for item in (before or []) if item not in after_set]
    return added, removed


async def user_label(
    self: "Handlers",
    user_id: uuid.UUID | None,
) -> str:
    if user_id is None or user_id.int == 0:
        return ""

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception:
        return str(user_id)

    display_name = getattr(user, "display_name", "") or ""
    if display_name:
        return display_name

    return str(getattr(user, "email", "") or user_id)


async def user_labels(
    self: "Handlers",
    user_ids: Iterable[uuid.UUID],
) -> list[str]:
    return [
        await user_label(self, user_id)
        for user_id in (user_ids or [])
    ]


async def label_name(
    self: "Handlers",
    project_id: uuid.UUID,
    label_id: uuid.UUID,
) -> str:
    try:
        labels = await self.store.tasks.list_labels(project_id)
    except Exception:
        return str(label_id)

    for label in labels:
        if label.id == label_id:
            return label.name

    return str(label_id)


async def record_task_diff(
    self: "Handlers",
    actor_id: uuid.UUID,
    before,
    after,
) -> None:
    if before is None or after is None:
        return

    async def log(field: str, old: str, new: str) -> None:
        if old == new:
            return
        try:
            await self.store.tasks.record_activity(
                after.id,
                actor_id,
                "field_changed",
                {
                    "field": field,
                    "from": old,
                    "to": new,
                },
            )
        except Exception:
            pass

    await log("title", before.title, after.title)
    await log("priority", before.priority, after.priority)
    await log("startDate", _fmt_date(before.start_date), _fmt_date(after.start_date))
    await log("dueDate", _fmt_date(before.due_date), _fmt_date(after.due_date))
    await log("startAt", _fmt_datetime(before.start_at), _fmt_datetime(after.start_at))
    await log("endAt", _fmt_datetime(before.end_at), _fmt_datetime(after.end_at))
    await log("storyPoints", _fmt_float(before.story_points), _fmt_float(after.story_points))
    await log("moscow", _deref_str(before.moscow), _deref_str(after.moscow))
    await log("sprint", _fmt_uuid(before.sprint_id), _fmt_uuid(after.sprint_id))

    if before.description != after.description:
        try:
            await self.store.tasks.record_activity(
                after.id,
                actor_id,
                "description_changed",
                None,
            )
        except Exception:
            pass

    if not _same_uuid(before.assignee_id, after.assignee_id):
        try:
            await self.store.tasks.record_activity(
                after.id,
                actor_id,
                "field_changed",
                {
                    "field": "assignee",
                    "from": await user_label(self, before.assignee_id),
                    "to": await user_label(self, after.assignee_id),
                    "fromId": _fmt_uuid(before.assignee_id),
                    "toId": _fmt_uuid(after.assignee_id),
                },
            )
        except Exception:
            pass

    if not _same_uuid(before.reporter_id, after.reporter_id):
        try:
            await self.store.tasks.record_activity(
                after.id,
                actor_id,
                "field_changed",
                {
                    "field": "reporter",
                    "from": await user_label(self, before.reporter_id),
                    "to": await user_label(self, after.reporter_id),
                },
            )
        except Exception:
            pass

    added, removed = diff_participants(
        before.participant_ids or [],
        after.participant_ids or [],
    )

    if added or removed:
        try:
            await self.store.tasks.record_activity(
                after.id,
                actor_id,
                "participants_changed",
                {
                    "added": await user_labels(self, added),
                    "removed": await user_labels(self, removed),
                },
            )
        except Exception:
            pass
