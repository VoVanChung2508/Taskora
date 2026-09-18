from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..util.slug import slugify

if TYPE_CHECKING:
    from .handlers import Handlers


logger = logging.getLogger(__name__)


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


async def _read_create_workspace_body(request: Request) -> str:
    try:
        data: Any = await request.json()
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

    unknown = set(data) - {"name"}

    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": f"unknown field(s): {sorted(unknown)}",
            },
        )

    name = data.get("name", "")

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    return name.strip()


def _is_unique_violation(exc: Exception) -> bool:
    """
    Postgres unique violation = SQLSTATE 23505.

    Giữ hành vi tương đương isUniqueViolation() trong Go.
    """
    return (
        getattr(exc, "sqlstate", None) == "23505"
        or "23505" in str(exc)
    )


async def create_workspace(
    self: "Handlers",
    request: Request,
):
    user_id = _current_user_id()

    # Chỉ system admin được tạo workspace.
    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only admins can create workspaces",
            },
        ) from exc

    if not user.is_system_admin:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only admins can create workspaces",
            },
        )

    name = await _read_create_workspace_body(request)

    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required",
            },
        )

    slug = slugify(name)

    try:
        workspace = await self.store.workspaces.create(
            name,
            slug,
            user_id,
        )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "slug_taken",
                    "message": "a workspace with a similar name exists",
                },
            ) from exc

        raise HTTPException(
            status_code=500,
            detail={
                "error": "create_failed",
                "message": str(exc),
            },
        ) from exc

    # SharePoint provisioning là best-effort:
    # lỗi SharePoint không được làm thất bại việc tạo workspace.
    if self.sharepoint is not None:
        try:
            folder_path = self.sharepoint.workspace_folder(
                workspace.slug
            )

            item = await self.sharepoint.ensure_folder(
                folder_path
            )

            if item is not None:
                try:
                    await self.store.workspaces.set_sharepoint_folder(
                        workspace.id,
                        folder_path,
                        item.id,
                    )

                    workspace.sharepoint_folder_path = folder_path
                    workspace.sharepoint_item_id = item.id

                except Exception:
                    logger.exception(
                        "failed to save SharePoint workspace folder metadata",
                        extra={
                            "workspace_id": str(workspace.id),
                        },
                    )

        except Exception:
            logger.exception(
                "failed to provision SharePoint workspace folder",
                extra={
                    "workspace_id": str(workspace.id),
                },
            )

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(workspace),
    )


async def list_workspaces(
    self: "Handlers",
) -> dict:
    user_id = _current_user_id()

    try:
        items = await self.store.workspaces.list_for_user(
            user_id
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
        "workspaces": items or [],
    }


async def get_workspace(
    self: "Handlers",
    workspace_id: uuid.UUID,
):
    user_id = _current_user_id()

    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    return workspace