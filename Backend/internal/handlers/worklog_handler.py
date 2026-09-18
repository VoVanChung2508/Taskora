from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.worklogs import NotFoundError as WorklogNotFoundError
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


DATE_FMT = "%Y-%m-%d"
VALID_WORKLOG_STATES = {"approved", "rejected", "submitted", "draft"}


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


def _parse_date(value: str) -> tuple[date | None, bool]:
    if value == "":
        return None, True

    try:
        return date.fromisoformat(value), True
    except (TypeError, ValueError):
        return None, False


def _parse_range(request: Request) -> tuple[date, date]:
    today = date.today()
    start = today - timedelta(days=today.weekday())  # Monday
    end = start + timedelta(days=6)

    raw_from = request.query_params.get("from")
    raw_to = request.query_params.get("to")

    if raw_from:
        parsed, ok = _parse_date(raw_from)
        if ok and parsed is not None:
            start = parsed

    if raw_to:
        parsed, ok = _parse_date(raw_to)
        if ok and parsed is not None:
            end = parsed

    return start, end


async def log_work(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    data = await _read_json_object(
        request,
        {"minutes", "note", "loggedOn", "source"},
    )

    minutes = data.get("minutes", 0)
    note = data.get("note", "")
    logged_on_raw = data.get("loggedOn", "")
    source = data.get("source", "")

    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "minutes must be an integer",
            },
        )

    if not isinstance(note, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "note must be a string"},
        )

    if not isinstance(logged_on_raw, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "loggedOn must be a string",
            },
        )

    if not isinstance(source, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "source must be a string"},
        )

    if minutes <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "minutes must be > 0"},
        )

    logged_on = date.today()
    if logged_on_raw:
        parsed, ok = _parse_date(logged_on_raw)
        if not ok or parsed is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation",
                    "message": "loggedOn must be YYYY-MM-DD",
                },
            )
        logged_on = parsed

    if source != "timer":
        source = "manual"

    try:
        worklog = await self.store.worklogs.add(
            task.id,
            user_id,
            minutes,
            note.strip(),
            source,
            logged_on,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "log_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "logged_time",
            {"minutes": minutes},
        )
    except Exception:
        pass

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(worklog),
    )


async def list_task_worklogs(
    self: "Handlers",
    task_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    try:
        items = await self.store.worklogs.list_by_task(task.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"worklogs": items or []}


async def my_timesheet(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()
    start, end = _parse_range(request)

    try:
        entries = await self.store.worklogs.timesheet_for_user(
            user_id,
            start,
            end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {
        "from": start.strftime(DATE_FMT),
        "to": end.strftime(DATE_FMT),
        "entries": entries or [],
    }


async def project_timesheet(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    project, role = await self.require_project_access(project_id, user_id)

    if role != "owner":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only owners can view team timesheet",
            },
        )

    start, end = _parse_range(request)

    try:
        entries = await self.store.worklogs.timesheet_for_project(
            project.id,
            start,
            end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {
        "from": start.strftime(DATE_FMT),
        "to": end.strftime(DATE_FMT),
        "entries": entries or [],
    }


async def my_calendar(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()
    start, end = _parse_range(request)

    try:
        items = await self.store.tasks.calendar_for_user(
            user_id,
            start,
            end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"tasks": items or []}


async def submit_timesheet(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()

    data = await _read_json_object(request, {"from", "to"})
    raw_from = data.get("from", "")
    raw_to = data.get("to", "")

    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "from and to must be strings",
            },
        )

    start, start_ok = _parse_date(raw_from)
    if not start_ok or start is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "from must be YYYY-MM-DD"},
        )

    end, end_ok = _parse_date(raw_to)
    if not end_ok or end is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "to must be YYYY-MM-DD"},
        )

    try:
        submitted = await self.store.worklogs.submit_range(
            user_id,
            start,
            end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "submit_failed", "message": str(exc)},
        ) from exc

    return {"submitted": submitted}


async def set_worklog_state(
    self: "Handlers",
    worklog_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()

    try:
        worklog = await self.store.worklogs.get_by_id(worklog_id)
        task = await self.store.tasks.get_by_id(worklog.task_id)
        project = await self.store.projects.get_by_id(task.project_id)
        role = await self.store.workspaces.role_for_user(
            project.workspace_id,
            user_id,
        )
    except WorklogNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "worklog not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "worklog not found"},
        ) from exc

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only managers can approve worklogs",
            },
        )

    data = await _read_json_object(request, {"state"})
    state = data.get("state", "")

    if not isinstance(state, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "state must be a string"},
        )

    if state not in VALID_WORKLOG_STATES:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "invalid state"},
        )

    try:
        await self.store.worklogs.set_state(worklog_id, state)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {
        "id": worklog_id,
        "state": state,
    }
