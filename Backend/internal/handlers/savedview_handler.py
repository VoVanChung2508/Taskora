from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from ..auth.session import get_user_id
from ..store.savedviews import NotFoundError
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


async def _read_create_body(request: Request) -> tuple[str, bool, dict[str, Any]]:
    """
    Đọc body gần tương đương httpx.Decode của Go:
    - JSON phải là object
    - không chấp nhận field lạ
    - kiểm tra type cơ bản
    """
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

    allowed = {"name", "shared", "config"}
    unknown = set(data) - allowed

    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": f"unknown field(s): {sorted(unknown)}",
            },
        )

    name = data.get("name", "")
    shared = data.get("shared", False)
    config = data.get("config", {})

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    if not isinstance(shared, bool):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "shared must be a boolean",
            },
        )

    if not isinstance(config, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "config must be an object",
            },
        )

    return name.strip(), shared, config


async def list_saved_views(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()

    project, _ = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        views = await self.store.saved_views.list_for_user(
            project.id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "list_failed",
                "message": str(exc),
            },
        ) from exc

    return {"views": views}


async def create_saved_view(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()

    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    name, shared, config = await _read_create_body(request)

    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required",
            },
        )

    owner: uuid.UUID | None = user_id

    if shared:
        if not can_manage_workspace(role):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "forbidden",
                    "message": "only owner/admin can save shared views",
                },
            )

        owner = None

    try:
        view = await self.store.saved_views.create(
            project.id,
            owner,
            name,
            config,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "create_failed",
                "message": str(exc),
            },
        ) from exc

    # Go trả 201 Created.
    from fastapi.responses import JSONResponse
    from fastapi.encoders import jsonable_encoder

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(view),
    )


async def delete_saved_view(
    self: "Handlers",
    project_id: uuid.UUID,
    view_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()

    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        await self.store.saved_views.delete(
            project.id,
            view_id,
            user_id,
            can_manage_workspace(role),
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "view does not exist or you cannot delete it",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "delete_failed",
                "message": str(exc),
            },
        ) from exc

    return {"ok": True}