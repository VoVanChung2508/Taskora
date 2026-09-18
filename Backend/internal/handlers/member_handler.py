from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.workspaces import NotFoundError as WorkspaceNotFoundError
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


VALID_WORKSPACE_ROLES = {
    "owner",
    "admin",
    "billing",
    "member",
    "guest",
}


def valid_workspace_role(role: str) -> bool:
    return role in VALID_WORKSPACE_ROLES


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
            detail={
                "error": "invalid_body",
                "message": str(exc),
            },
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "expected JSON object",
            },
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


async def list_members(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()

    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    try:
        members = await self.store.workspaces.list_members(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "list_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "members": members or [],
    }


async def project_members(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()

    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        members = await self.store.workspaces.list_members(
            project.workspace_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "list_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "members": members or [],
    }


async def add_member(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()

    workspace, role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only owner/admin can add members",
            },
        )

    data = await _read_json_object(
        request,
        {"email", "role"},
    )

    email = data.get("email") or ""
    requested_role = data.get("role") or ""

    if not isinstance(email, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "email must be a string",
            },
        )

    if not isinstance(requested_role, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "role must be a string",
            },
        )

    email = email.strip()

    if requested_role == "":
        requested_role = "member"

    if not email or not valid_workspace_role(requested_role):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "valid email and role required",
            },
        )

    try:
        member = await self.store.workspaces.add_member_by_email(
            workspace.id,
            email,
            requested_role,
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "user_not_found",
                "message": "no user with that email has signed in yet",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "add_failed",
                "message": str(exc),
            },
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(member),
    )


async def update_member(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
) -> dict:
    current_user_id = _current_user_id()

    workspace, role = await self.require_workspace_member(
        workspace_id,
        current_user_id,
    )

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only owner/admin can change roles",
            },
        )

    data = await _read_json_object(
        request,
        {"role"},
    )

    requested_role = data.get("role") or ""

    if not isinstance(requested_role, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "role must be a string",
            },
        )

    if not valid_workspace_role(requested_role):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "invalid role",
            },
        )

    try:
        await self.store.workspaces.update_member_role(
            workspace.id,
            user_id,
            requested_role,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "update_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "userId": user_id,
        "role": requested_role,
    }


async def set_member_rate(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
) -> dict:
    current_user_id = _current_user_id()

    _workspace, role = await self.require_workspace_member(
        workspace_id,
        current_user_id,
    )

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only owner/admin can set rates",
            },
        )

    data = await _read_json_object(
        request,
        {"hourlyRate", "currency"},
    )

    hourly_rate = data.get("hourlyRate", 0)
    currency = data.get("currency") or "USD"

    if (
        isinstance(hourly_rate, bool)
        or not isinstance(hourly_rate, (int, float))
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "hourlyRate must be a number",
            },
        )

    if not isinstance(currency, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "currency must be a string",
            },
        )

    try:
        await self.store.workspaces.set_rate(
            user_id,
            float(hourly_rate),
            currency,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "update_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "userId": user_id,
        "hourlyRate": float(hourly_rate),
        "currency": currency,
    }