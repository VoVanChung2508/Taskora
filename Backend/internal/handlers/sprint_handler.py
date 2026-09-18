from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.sprints import SprintUpdateFields

if TYPE_CHECKING:
    from .handlers import Handlers


logger = logging.getLogger(__name__)
VALID_SPRINT_STATES = {"planned", "active", "completed"}


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": "missing authenticated user"},
        )
    return user_id


async def _read_json_object(request: Request, allowed_fields: set[str]) -> dict[str, Any]:
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
            detail={"error": "invalid_body", "message": f"unknown field(s): {sorted(unknown)}"},
        )
    return data


def _parse_date(value: str) -> tuple[date | None, bool]:
    if value == "":
        return None, True
    try:
        return datetime.strptime(value, "%Y-%m-%d").date(), True
    except (TypeError, ValueError):
        return None, False


async def require_sprint_access(
    self: "Handlers",
    sprint_id: uuid.UUID,
    user_id: uuid.UUID,
):
    try:
        sprint = await self.store.sprints.get_by_id(sprint_id)
        project = await self.store.projects.get_by_id(sprint.project_id)
        await self.store.workspaces.role_for_user(project.workspace_id, user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "sprint not found"},
        ) from exc
    return sprint


async def create_sprint(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(project_id, user_id)

    if role in {"guest", "billing"}:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "insufficient role"},
        )

    data = await _read_json_object(
        request,
        {"name", "goal", "startDate", "endDate"},
    )
    name = data.get("name", "")
    goal = data.get("goal", "")
    start_raw = data.get("startDate", "")
    end_raw = data.get("endDate", "")

    if not isinstance(name, str) or not isinstance(goal, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "name and goal must be strings"},
        )
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "startDate and endDate must be strings"},
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "name is required"},
        )

    start_date, start_ok = _parse_date(start_raw)
    end_date, end_ok = _parse_date(end_raw)
    if not start_ok or not end_ok:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "dates must be YYYY-MM-DD"},
        )
    if start_date is not None and end_date is not None and end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "end date must not precede start date"},
        )

    try:
        sprint = await self.store.sprints.create(project.id, name, goal.strip())
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    if start_date is not None or end_date is not None:
        fields = SprintUpdateFields(
            set_start_date=start_date is not None,
            start_date=start_date,
            set_end_date=end_date is not None,
            end_date=end_date,
        )
        try:
            sprint = await self.store.sprints.update(sprint.id, fields)
        except Exception:
            logger.exception("apply sprint dates failed: sprint=%s", sprint.id)

    return JSONResponse(status_code=201, content=jsonable_encoder(sprint))


async def list_sprints(self: "Handlers", project_id: uuid.UUID) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(project_id, user_id)
    try:
        sprints = await self.store.sprints.list_by_project(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc
    return {"sprints": sprints or []}


async def sprint_burndown(self: "Handlers", sprint_id: uuid.UUID):
    user_id = _current_user_id()
    sprint = await require_sprint_access(self, sprint_id, user_id)
    try:
        return await self.store.sprints.burndown(sprint.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "burndown_failed", "message": str(exc)},
        ) from exc


async def sprint_capacity(self: "Handlers", sprint_id: uuid.UUID):
    user_id = _current_user_id()
    sprint = await require_sprint_access(self, sprint_id, user_id)
    try:
        return await self.store.sprints.capacity(sprint.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "capacity_failed", "message": str(exc)},
        ) from exc


async def project_velocity(self: "Handlers", project_id: uuid.UUID) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(project_id, user_id)
    try:
        velocity = await self.store.sprints.velocity(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "velocity_failed", "message": str(exc)},
        ) from exc
    return {"sprints": velocity or []}


async def update_sprint(
    self: "Handlers",
    sprint_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    await require_sprint_access(self, sprint_id, user_id)

    data = await _read_json_object(
        request,
        {"name", "goal", "state", "startDate", "endDate"},
    )

    name = data.get("name")
    goal = data.get("goal")
    state = data.get("state")

    if name is not None and not isinstance(name, str):
        raise HTTPException(400, detail={"error": "invalid_body", "message": "name must be a string"})
    if goal is not None and not isinstance(goal, str):
        raise HTTPException(400, detail={"error": "invalid_body", "message": "goal must be a string"})
    if state is not None:
        if not isinstance(state, str):
            raise HTTPException(400, detail={"error": "invalid_body", "message": "state must be a string"})
        if state not in VALID_SPRINT_STATES:
            raise HTTPException(400, detail={"error": "validation", "message": "invalid state"})

    fields = SprintUpdateFields(name=name, goal=goal, state=state)

    if "startDate" in data and data["startDate"] is not None:
        raw = data["startDate"]
        if not isinstance(raw, str):
            raise HTTPException(
                400,
                detail={"error": "invalid_body", "message": "startDate must be a string or null"},
            )
        parsed, valid = _parse_date(raw)
        if valid:
            fields.set_start_date = True
            fields.start_date = parsed

    if "endDate" in data and data["endDate"] is not None:
        raw = data["endDate"]
        if not isinstance(raw, str):
            raise HTTPException(
                400,
                detail={"error": "invalid_body", "message": "endDate must be a string or null"},
            )
        parsed, valid = _parse_date(raw)
        if valid:
            fields.set_end_date = True
            fields.end_date = parsed

    try:
        return await self.store.sprints.update(sprint_id, fields)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc


async def sprint_name(self: "Handlers", sprint_id: uuid.UUID | None) -> str:
    if sprint_id is None:
        return "Backlog"
    try:
        sprint = await self.store.sprints.get_by_id(sprint_id)
        return sprint.name
    except Exception:
        return ""


async def set_task_sprint(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    task, role = await self.require_task_access(task_id, user_id)

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "guests cannot plan sprints"},
        )

    data = await _read_json_object(request, {"sprintId"})
    raw_sprint_id = data.get("sprintId")

    if raw_sprint_id is None:
        sprint_id = None
    elif isinstance(raw_sprint_id, str):
        try:
            sprint_id = uuid.UUID(raw_sprint_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "invalid sprintId"},
            ) from exc
    else:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "sprintId must be a UUID string or null"},
        )

    old_name = await sprint_name(self, task.sprint_id)
    new_name = await sprint_name(self, sprint_id)

    try:
        await self.store.tasks.set_sprint(task.id, sprint_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "field_changed",
            {"field": "sprint", "from": old_name, "to": new_name},
        )
    except Exception:
        logger.exception("record sprint activity failed: task=%s", task.id)

    return {"ok": True}
