from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.workflow import LastStatusError, NotFoundError, StatusUpdateFields
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


STATUS_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

VALID_CATEGORIES = {"todo", "in_progress", "done"}
DEFAULT_STATUS_COLOR = "#3b82f6"
LEGACY_STATUS_COLORS = {"blue", "purple", "orange", "green", "red", "gray"}


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


def valid_status_color(color: str) -> bool:
    return bool(HEX_COLOR_RE.fullmatch(color)) or color in LEGACY_STATUS_COLORS


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) == "23505" or "23505" in str(exc)


async def list_statuses(self: "Handlers", project_id: uuid.UUID) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(project_id, user_id)
    try:
        statuses = await self.store.tasks.list_statuses(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc
    return {"statuses": statuses or []}


async def create_status(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(project_id, user_id)

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "requires workspace owner or admin"},
        )

    data = await _read_json_object(
        request,
        {"key", "name", "category", "color", "wipLimit"},
    )

    key = data.get("key", "")
    name = data.get("name", "")
    category = data.get("category", "")
    color = data.get("color", "")
    wip_limit = data.get("wipLimit")

    for field_name, value in (
        ("key", key),
        ("name", name),
        ("category", category),
        ("color", color),
    ):
        if not isinstance(value, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": f"{field_name} must be a string"},
            )

    if wip_limit is not None and (
        isinstance(wip_limit, bool) or not isinstance(wip_limit, int)
    ):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "wipLimit must be an integer or null"},
        )

    name = name.strip()
    key = key.strip().lower()

    if key == "":
        key = name.replace(" ", "_").lower()
        key = re.sub(r"[^a-z0-9_]", "", key)

    if not name or not STATUS_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required and key must match [a-z0-9_]{1,32}",
            },
        )

    if category == "":
        category = "todo"
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "category must be todo, in_progress or done",
            },
        )

    if color == "":
        color = DEFAULT_STATUS_COLOR
    if not valid_status_color(color):
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "color must be a hex value like #3b82f6"},
        )

    if wip_limit is not None and wip_limit < 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "wipLimit must be >= 0"},
        )

    try:
        status = await self.store.tasks.create_status(
            project.id,
            key,
            name,
            category,
            color,
            wip_limit,
        )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise HTTPException(
                status_code=409,
                detail={"error": "status_exists", "message": "key already used in this project"},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(status_code=201, content=jsonable_encoder(status))


async def update_status(
    self: "Handlers",
    project_id: uuid.UUID,
    status_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    project, role = await self.require_project_access(project_id, user_id)

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "requires workspace owner or admin"},
        )

    data = await _read_json_object(
        request,
        {"name", "category", "color", "position", "wipLimit", "clearWip"},
    )

    name = data.get("name")
    category = data.get("category")
    color = data.get("color")
    position = data.get("position")
    wip_limit = data.get("wipLimit")
    clear_wip = data.get("clearWip", False)

    if name is not None and not isinstance(name, str):
        raise HTTPException(400, detail={"error": "invalid_body", "message": "name must be a string"})

    if category is not None:
        if not isinstance(category, str):
            raise HTTPException(
                400,
                detail={"error": "invalid_body", "message": "category must be a string"},
            )
        if category not in VALID_CATEGORIES:
            raise HTTPException(
                400,
                detail={"error": "validation", "message": "invalid category"},
            )

    if color is not None:
        if not isinstance(color, str):
            raise HTTPException(
                400,
                detail={"error": "invalid_body", "message": "color must be a string"},
            )
        if not valid_status_color(color):
            raise HTTPException(
                400,
                detail={"error": "validation", "message": "color must be a hex value like #3b82f6"},
            )

    if position is not None and (
        isinstance(position, bool) or not isinstance(position, (int, float))
    ):
        raise HTTPException(
            400,
            detail={"error": "invalid_body", "message": "position must be a number"},
        )

    if wip_limit is not None and (
        isinstance(wip_limit, bool) or not isinstance(wip_limit, int)
    ):
        raise HTTPException(
            400,
            detail={"error": "invalid_body", "message": "wipLimit must be an integer or null"},
        )

    if not isinstance(clear_wip, bool):
        raise HTTPException(
            400,
            detail={"error": "invalid_body", "message": "clearWip must be a boolean"},
        )

    fields = StatusUpdateFields(
        name=name,
        category=category,
        color=color,
        position=float(position) if position is not None else None,
    )

    if clear_wip:
        fields.set_wip_limit = True
        fields.wip_limit = None
    elif wip_limit is not None:
        fields.set_wip_limit = True
        fields.wip_limit = wip_limit

    try:
        await self.store.tasks.update_workflow_status(project.id, status_id, fields)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "status not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def delete_status(
    self: "Handlers",
    project_id: uuid.UUID,
    status_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, role = await self.require_project_access(project_id, user_id)

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "requires workspace owner or admin"},
        )

    try:
        await self.store.tasks.delete_status(project.id, status_id)
    except LastStatusError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "last_status", "message": "không thể xoá cột cuối cùng"},
        ) from exc
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "status not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def check_wip_limit(
    self: "Handlers",
    project,
    status_key: str,
) -> tuple[bool, int, int]:
    try:
        limit = await self.store.tasks.wip_limit_for(project.id, status_key)
    except Exception:
        return False, 0, 0

    if limit is None or limit <= 0:
        return False, 0, 0

    try:
        count = await self.store.tasks.count_tasks_in_status(project.id, status_key)
    except Exception:
        return False, 0, 0

    return count >= limit, limit, count
