from __future__ import annotations

import inspect
import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.tasks import CreateTaskParams, TaskUpdateFields
from .task_diff import (
    comment_link,
    label_name,
    record_task_diff,
    task_link,
)

if TYPE_CHECKING:
    from .handlers import Handlers


logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"@([\w.+\-]+@[\w.\-]+|[\w]+)", re.UNICODE)
VALID_MOSCOW = {"must", "should", "could", "wont"}


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthenticated",
                "message": "missing authenticated user",
            },
        )
    return user_id


async def _read_json_object(
    request: Request,
    allowed_fields: set[str],
) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": str(exc)},
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "expected JSON object"},
        )

    unknown = set(data) - allowed_fields
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": f"unknown field(s): {sorted(unknown)}",
            },
        )
    return data


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": f"{field} must be a UUID string"},
        )
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": f"invalid {field}"},
        ) from exc


def _parse_date(value: str) -> tuple[date | None, bool]:
    if value == "":
        return None, True
    try:
        return datetime.strptime(value, "%Y-%m-%d").date(), True
    except (TypeError, ValueError):
        return None, False


def _parse_datetime(value: str) -> tuple[datetime | None, bool]:
    if value == "":
        return None, True

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, False

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed, True


async def _maybe_emit(
    self: "Handlers",
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    event: str,
    payload: dict[str, Any],
) -> None:
    fn = getattr(self, "emit", None)
    if fn is None:
        return
    try:
        result = fn(project_id, actor_id, event, payload)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("emit failed: event=%s project=%s", event, project_id)


async def _maybe_run_automations(
    self: "Handlers",
    task,
    actor_id: uuid.UUID,
) -> None:
    fn = getattr(self, "run_automations", None)
    if fn is None:
        return
    try:
        result = fn(task, actor_id)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("run automations failed: task=%s", task.id)


def parse_mentions(body: str) -> set[str]:
    return {match.lower() for match in MENTION_RE.findall(body)}


async def notify_mentions(
    self: "Handlers",
    task,
    body: str,
    notified: set[uuid.UUID],
    comment_id: uuid.UUID,
) -> None:
    tokens = parse_mentions(body)
    if not tokens:
        return

    try:
        project = await self.store.projects.get_by_id(task.project_id)
        members = await self.store.workspaces.list_members(project.workspace_id)
    except Exception:
        return

    for member in members:
        if member.user_id in notified:
            continue

        email = (member.email or "").lower()
        local = email.split("@", 1)[0] if "@" in email else email

        display_name = member.display_name or ""
        first = display_name.split()[0].lower() if display_name.split() else ""

        if (
            email in tokens
            or local in tokens
            or (first and first in tokens)
        ):
            try:
                await self.store.notifications.create(
                    member.user_id,
                    "mentioned",
                    "Bạn được nhắc đến trong một bình luận",
                    task.title,
                    task.id,
                    comment_link(task.project_id, task.id, comment_id),
                )
                notified.add(member.user_id)
            except Exception:
                pass


async def create_task(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(project_id, user_id)

    await self.require_permission(
        project.workspace_id,
        user_id,
        role,
        "task.create",
    )

    data = await _read_json_object(
        request,
        {
            "title",
            "description",
            "status",
            "priority",
            "assigneeId",
            "parentTaskId",
            "participantIds",
        },
    )

    title = data.get("title", "")
    description = data.get("description", "")
    status = data.get("status", "")
    priority = data.get("priority", "")

    for field, value in (
        ("title", title),
        ("description", description),
        ("status", status),
        ("priority", priority),
    ):
        if not isinstance(value, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": f"{field} must be a string"},
            )

    title = title.strip()
    if not title:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "title is required"},
        )

    assignee_id = None
    if data.get("assigneeId") is not None:
        assignee_id = _parse_uuid(data["assigneeId"], "assigneeId")

    parent_task_id = None
    if data.get("parentTaskId") is not None:
        parent_task_id = _parse_uuid(data["parentTaskId"], "parentTaskId")

    participant_ids: list[uuid.UUID] = []
    raw_participants = data.get("participantIds", [])
    if raw_participants is None:
        raw_participants = []
    if not isinstance(raw_participants, list):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "participantIds must be an array"},
        )
    for item in raw_participants:
        participant_ids.append(_parse_uuid(item, "participantIds"))

    try:
        task = await self.store.tasks.create(
            CreateTaskParams(
                project_id=project.id,
                parent_task_id=parent_task_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                assignee_id=assignee_id,
                reporter_id=user_id,
                participant_ids=participant_ids,
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "created",
            {"title": task.title},
        )
    except Exception:
        pass

    await _maybe_emit(
        self,
        project.id,
        user_id,
        "task.created",
        {"taskId": task.id},
    )

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(task),
    )


async def delete_task(
    self: "Handlers",
    task_id: uuid.UUID,
):
    user_id = _current_user_id()
    task, role = await self.require_task_access(task_id, user_id)

    try:
        project = await self.store.projects.get_by_id(task.project_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "project not found"},
        ) from exc

    await self.require_permission(
        project.workspace_id,
        user_id,
        role,
        "task.delete",
    )

    try:
        await self.store.tasks.delete(task.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    await _maybe_emit(
        self,
        task.project_id,
        user_id,
        "task.deleted",
        {"taskId": task.id},
    )

    return Response(status_code=204)


async def list_tasks(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(project_id, user_id)

    try:
        items = await self.store.tasks.list_by_project_enriched(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"tasks": items or []}


async def update_task_status(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    data = await _read_json_object(request, {"status"})
    status = data.get("status", "")

    if not isinstance(status, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "status must be a string"},
        )

    status = status.strip()
    if not status:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "status is required"},
        )

    if task.status != status:
        try:
            project = await self.store.projects.get_by_id(task.project_id)
        except Exception:
            project = None

        if project is not None:
            exceeded, limit, count = await self.check_wip_limit(project, status)
            if exceeded:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "wip_limit_exceeded",
                        "message": f"cột này giới hạn {limit} công việc (hiện có {count})",
                    },
                )

    try:
        updated = await self.store.tasks.update_status(task.id, status)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    if task.status != status:
        try:
            await self.store.tasks.record_activity(
                task.id,
                user_id,
                "status_changed",
                {"from": task.status, "to": status},
            )
        except Exception:
            pass

        await _maybe_run_automations(self, updated, user_id)
        await _maybe_emit(
            self,
            task.project_id,
            user_id,
            "task.status_changed",
            {
                "taskId": task.id,
                "from": task.status,
                "to": status,
            },
        )

    return updated


async def _best_effort(awaitable, default):
    try:
        return await awaitable
    except Exception:
        return default


async def get_task(
    self: "Handlers",
    task_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    comments = await _best_effort(
        self.store.tasks.list_comments(task.id),
        [],
    )
    checklist = await _best_effort(
        self.store.tasks.list_checklist(task.id),
        [],
    )
    activity = await _best_effort(
        self.store.tasks.list_activity(task.id),
        [],
    )
    labels = await _best_effort(
        self.store.tasks.list_labels(task.project_id),
        [],
    )
    dependencies = await _best_effort(
        self.store.tasks.list_dependencies(task.id),
        None,
    )
    custom_fields = await _best_effort(
        self.store.tasks.list_custom_field_values(task.id),
        [],
    )

    return {
        "task": task,
        "comments": comments,
        "checklist": checklist,
        "activity": activity,
        "labels": labels,
        "dependencies": dependencies,
        "customFields": custom_fields,
    }


async def update_task(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, role = await self.require_task_access(task_id, user_id)

    try:
        project = await self.store.projects.get_by_id(task.project_id)
    except Exception:
        project = None

    if project is not None:
        await self.require_permission(
            project.workspace_id,
            user_id,
            role,
            "task.edit",
        )

    data = await _read_json_object(
        request,
        {
            "title",
            "description",
            "priority",
            "storyPoints",
            "startDate",
            "dueDate",
            "assigneeId",
            "reporterId",
            "participantIds",
            "startAt",
            "endAt",
            "moscow",
            "riceReach",
            "riceImpact",
            "riceConfidence",
            "riceEffort",
        },
    )

    fields = TaskUpdateFields()

    for key, attr in (
        ("title", "title"),
        ("description", "description"),
        ("priority", "priority"),
    ):
        if key in data and data[key] is not None:
            if not isinstance(data[key], str):
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_body", "message": f"{key} must be a string"},
                )
            setattr(fields, attr, data[key])

    if data.get("storyPoints") is not None:
        value = data["storyPoints"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "storyPoints must be a number"},
            )
        fields.set_story_points = True
        fields.story_points = float(value)

    if data.get("startDate") is not None:
        raw = data["startDate"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "startDate must be a string"},
            )
        parsed, valid = _parse_date(raw)
        if valid:
            fields.set_start_date = True
            fields.start_date = parsed

    if data.get("dueDate") is not None:
        raw = data["dueDate"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "dueDate must be a string"},
            )
        parsed, valid = _parse_date(raw)
        if valid:
            fields.set_due_date = True
            fields.due_date = parsed

    if data.get("assigneeId") is not None:
        raw = data["assigneeId"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "assigneeId must be a string"},
            )
        fields.set_assignee = True
        fields.assignee_id = None
        if raw != "":
            try:
                fields.assignee_id = uuid.UUID(raw)
            except ValueError:
                # Fidelity with Go: invalid non-empty UUID leaves nil while
                # SetAssignee remains true, so the field is cleared.
                fields.assignee_id = None

    if data.get("reporterId") is not None:
        raw = data["reporterId"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "reporterId must be a string"},
            )
        fields.set_reporter = True
        fields.reporter_id = None
        if raw != "":
            try:
                fields.reporter_id = uuid.UUID(raw)
            except ValueError:
                fields.reporter_id = None

    if data.get("participantIds") is not None:
        raw = data["participantIds"]
        if not isinstance(raw, list):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "participantIds must be an array"},
            )
        fields.set_participants = True
        fields.participant_ids = []
        for item in raw:
            if not isinstance(item, str):
                continue
            try:
                fields.participant_ids.append(uuid.UUID(item))
            except ValueError:
                continue

    if data.get("startAt") is not None:
        raw = data["startAt"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "startAt must be a string"},
            )
        parsed, valid = _parse_datetime(raw)
        if valid:
            fields.set_start_at = True
            fields.start_at = parsed

    if data.get("endAt") is not None:
        raw = data["endAt"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "endAt must be a string"},
            )
        parsed, valid = _parse_datetime(raw)
        if valid:
            fields.set_end_at = True
            fields.end_at = parsed

    if data.get("moscow") is not None:
        raw = data["moscow"]
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "moscow must be a string"},
            )

        moscow = raw.strip().lower()
        if moscow and moscow not in VALID_MOSCOW:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation",
                    "message": "moscow must be must, should, could or wont",
                },
            )

        fields.set_moscow = True
        fields.moscow = moscow or None

    rice_keys = (
        "riceReach",
        "riceImpact",
        "riceConfidence",
        "riceEffort",
    )
    if any(data.get(key) is not None for key in rice_keys):
        for key in rice_keys:
            value = data.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_body", "message": f"{key} must be a number"},
                )

        fields.set_rice = True
        fields.rice_reach = (
            float(data["riceReach"]) if data.get("riceReach") is not None else None
        )
        fields.rice_impact = (
            float(data["riceImpact"]) if data.get("riceImpact") is not None else None
        )
        fields.rice_confidence = (
            float(data["riceConfidence"]) if data.get("riceConfidence") is not None else None
        )
        fields.rice_effort = (
            float(data["riceEffort"]) if data.get("riceEffort") is not None else None
        )

    try:
        updated = await self.store.tasks.update(task.id, fields)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    await record_task_diff(self, user_id, task, updated)

    if (
        data.get("assigneeId") is not None
        and fields.assignee_id is not None
        and fields.assignee_id != user_id
    ):
        try:
            await self.store.notifications.create(
                fields.assignee_id,
                "assigned",
                "Bạn được giao một công việc",
                task.title,
                task.id,
                task_link(task.project_id, task.id),
            )
        except Exception:
            pass

    await _maybe_emit(
        self,
        task.project_id,
        user_id,
        "task.updated",
        {"taskId": task.id},
    )

    return updated


async def add_comment(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    data = await _read_json_object(request, {"body"})
    body = data.get("body", "")

    if not isinstance(body, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "body must be a string"},
        )

    body = body.strip()
    if not body:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "body is required"},
        )

    try:
        comment = await self.store.tasks.add_comment(
            task.id,
            user_id,
            body,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "comment_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "commented",
            None,
        )
    except Exception:
        pass

    notified: set[uuid.UUID] = {user_id}

    if (
        task.assignee_id is not None
        and task.assignee_id not in notified
    ):
        try:
            await self.store.notifications.create(
                task.assignee_id,
                "commented",
                "Bình luận mới trên công việc của bạn",
                task.title,
                task.id,
                comment_link(task.project_id, task.id, comment.id),
            )
            notified.add(task.assignee_id)
        except Exception:
            pass

    await notify_mentions(
        self,
        task,
        body,
        notified,
        comment.id,
    )

    await _maybe_emit(
        self,
        task.project_id,
        user_id,
        "task.commented",
        {"taskId": task.id},
    )

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(comment),
    )


async def add_checklist_item(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    data = await _read_json_object(request, {"title", "done"})
    title = data.get("title", "")

    if not isinstance(title, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "title must be a string"},
        )

    title = title.strip()
    if not title:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "title is required"},
        )

    try:
        item = await self.store.tasks.add_checklist_item(
            task.id,
            title,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "checklist_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(item),
    )


async def toggle_checklist_item(
    self: "Handlers",
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    await self.require_task_access(task_id, user_id)

    data = await _read_json_object(request, {"title", "done"})
    done = data.get("done")
    done = done if isinstance(done, bool) else False

    try:
        await self.store.tasks.toggle_checklist_item(
            item_id,
            done,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {
        "id": item_id,
        "done": done,
    }


async def list_labels(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(project_id, user_id)

    try:
        labels = await self.store.tasks.list_labels(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"labels": labels or []}


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) == "23505" or "23505" in str(exc)


async def create_label(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, _role = await self.require_project_access(project_id, user_id)

    data = await _read_json_object(request, {"name", "color"})
    name = data.get("name", "")
    color = data.get("color", "")

    if not isinstance(name, str) or not isinstance(color, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name and color must be strings",
            },
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "name is required"},
        )

    if not color:
        color = "primary"

    try:
        label = await self.store.tasks.create_label(
            project.id,
            name,
            color,
        )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "label_exists",
                    "message": "label name already used",
                },
            ) from exc

        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(label),
    )


async def set_task_label(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    data = await _read_json_object(request, {"labelId", "on"})

    raw_label_id = data.get("labelId")
    if raw_label_id is None:
        label_id = uuid.UUID(int=0)
    else:
        label_id = _parse_uuid(raw_label_id, "labelId")

    on = data.get("on", False)
    if not isinstance(on, bool):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "on must be a boolean"},
        )

    try:
        await self.store.tasks.set_task_label(
            task.id,
            label_id,
            on,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    verb = "label_added" if on else "label_removed"

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            verb,
            {
                "label": await label_name(
                    self,
                    task.project_id,
                    label_id,
                )
            },
        )
    except Exception:
        pass

    return {"ok": True}
