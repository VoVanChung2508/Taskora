from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.iam import NotFoundError as IAMNotFoundError
from ..store.audit import AUDIT_CUSTOM_ROLE_SET
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


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


def _permission_catalogue() -> list[str]:
    """
    Load the Python port of Go domain.AllPermissions without duplicating the
    catalogue inside the handler.
    """
    from ..domain import models as domain_models

    for attr in (
        "ALL_PERMISSIONS",
        "AllPermissions",
        "all_permissions",
    ):
        value = getattr(domain_models, attr, None)
        if value is not None:
            return [str(item) for item in value]

    raise RuntimeError("permission catalogue not found in domain.models")


def valid_permissions(values: list[str]) -> list[str]:
    allowed = set(_permission_catalogue())
    seen: set[str] = set()
    out: list[str] = []

    for value in values:
        permission = value.strip()
        if permission in allowed and permission not in seen:
            out.append(permission)
            seen.add(permission)

    return out


async def require_workspace_manager(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
):
    workspace, role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "requires workspace owner or admin",
            },
        )

    return workspace


async def list_permissions(self: "Handlers") -> dict:
    try:
        permissions = _permission_catalogue()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "permission_catalogue_missing",
                "message": str(exc),
            },
        ) from exc

    return {"permissions": permissions}


async def list_custom_roles(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    try:
        roles = await self.store.workspaces.list_custom_roles(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"roles": roles or []}


async def create_custom_role(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    data = await _read_json_object(
        request,
        {"name", "permissions"},
    )

    name = data.get("name", "")
    permissions = data.get("permissions", [])

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    if permissions is None:
        permissions = []

    if not isinstance(permissions, list) or not all(
        isinstance(item, str) for item in permissions
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "permissions must be an array of strings",
            },
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required",
            },
        )

    try:
        filtered_permissions = valid_permissions(permissions)
        role = await self.store.workspaces.create_custom_role(
            workspace.id,
            name,
            filtered_permissions,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "permission_catalogue_missing",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(role),
    )


async def update_custom_role(
    self: "Handlers",
    workspace_id: uuid.UUID,
    role_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    data = await _read_json_object(
        request,
        {"name", "permissions"},
    )

    name = data.get("name", "")
    permissions = data.get("permissions", [])

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    if permissions is None:
        permissions = []

    if not isinstance(permissions, list) or not all(
        isinstance(item, str) for item in permissions
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "permissions must be an array of strings",
            },
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required",
            },
        )

    try:
        filtered_permissions = valid_permissions(permissions)
        await self.store.workspaces.update_custom_role(
            workspace.id,
            role_id,
            name,
            filtered_permissions,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "permission_catalogue_missing",
                "message": str(exc),
            },
        ) from exc
    except IAMNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "role not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def delete_custom_role(
    self: "Handlers",
    workspace_id: uuid.UUID,
    role_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        await self.store.workspaces.delete_custom_role(
            workspace.id,
            role_id,
        )
    except IAMNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "role not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def assign_custom_role(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
) -> dict:
    actor_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        actor_id,
    )

    data = await _read_json_object(request, {"roleId"})
    raw_role_id = data.get("roleId")

    role_id: uuid.UUID | None = None

    if raw_role_id not in (None, ""):
        if not isinstance(raw_role_id, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_id",
                    "message": "roleId must be a valid uuid",
                },
            )

        try:
            role_id = uuid.UUID(raw_role_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_id",
                    "message": "roleId must be a valid uuid",
                },
            ) from exc

    await self.audit(
        request,
        AUDIT_CUSTOM_ROLE_SET,
        str(user_id),
        workspace.id,
        {"roleId": raw_role_id},
    )

    try:
        await self.store.workspaces.assign_custom_role(
            workspace.id,
            user_id,
            role_id,
        )
    except IAMNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "member not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def list_teams(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    try:
        teams = await self.store.workspaces.list_teams(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"teams": teams or []}


async def create_team(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    data = await _read_json_object(request, {"name"})
    name = data.get("name", "")

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required",
            },
        )

    try:
        team = await self.store.workspaces.create_team(
            workspace.id,
            name,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(team),
    )


async def delete_team(
    self: "Handlers",
    workspace_id: uuid.UUID,
    team_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        await self.store.workspaces.delete_team(
            workspace.id,
            team_id,
        )
    except IAMNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "team not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def set_team_member(
    self: "Handlers",
    workspace_id: uuid.UUID,
    team_id: uuid.UUID,
    request: Request,
) -> dict:
    actor_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        actor_id,
    )

    data = await _read_json_object(
        request,
        {"userId", "on"},
    )

    raw_user_id = data.get("userId")
    on = data.get("on", False)

    if not isinstance(raw_user_id, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "userId is required",
            },
        )

    try:
        target_user_id = uuid.UUID(raw_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "userId must be a UUID",
            },
        ) from exc

    if target_user_id.int == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "userId is required",
            },
        )

    if not isinstance(on, bool):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "on must be a boolean",
            },
        )

    try:
        await self.store.workspaces.set_team_member(
            workspace.id,
            team_id,
            target_user_id,
            on,
        )
    except IAMNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "team or member not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
