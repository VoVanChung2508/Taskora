from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.projects import CreateProjectParams
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


def _is_unique_violation(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)

    if sqlstate == "23505":
        return True

    text = str(exc)
    return "23505" in text or "SQLSTATE 23505" in text


async def _read_create_project_body(
    request: Request,
) -> tuple[str, str, str]:
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

    allowed = {
        "name",
        "key",
        "description",
    }

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
    key = data.get("key", "")
    description = data.get("description", "")

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    if not isinstance(key, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "key must be a string",
            },
        )

    if not isinstance(description, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "description must be a string",
            },
        )

    return (
        name.strip(),
        key.strip().upper(),
        description,
    )


def _default_project_subfolders() -> tuple[str, ...]:
    """
    Migration-safe lookup cho Go sharepoint.DefaultProjectSubfolders.

    Không tự bịa tên folder nếu constant chưa được port sang Python.
    """
    try:
        from ..storage.sharepoint import upload as sharepoint_upload

        value = getattr(
            sharepoint_upload,
            "DEFAULT_PROJECT_SUBFOLDERS",
            None,
        )

        if value is None:
            value = getattr(
                sharepoint_upload,
                "DefaultProjectSubfolders",
                None,
            )

        if value:
            return tuple(str(item) for item in value)

    except Exception:
        logger.exception(
            "failed to load SharePoint default project subfolders"
        )

    return ()


async def provision_project_folder(
    self: "Handlers",
    workspace_slug: str,
    project,
) -> None:
    if self.sharepoint is None:
        return

    project_slug = f"{project.key}-{slugify(project.name)}"

    try:
        base = self.sharepoint.project_folder(
            workspace_slug,
            project_slug,
        )

        item = await self.sharepoint.ensure_folder(base)

        if item is None:
            logger.error(
                "provision project folder returned no item: project=%s",
                project.id,
            )
            return

    except Exception:
        logger.exception(
            "provision project folder failed: project=%s",
            project.id,
        )
        return

    for subfolder in _default_project_subfolders():
        try:
            await self.sharepoint.ensure_folder(
                f"{base}/{subfolder}"
            )
        except Exception:
            logger.exception(
                "provision project subfolder failed: "
                "project=%s sub=%s",
                project.id,
                subfolder,
            )

    try:
        await self.store.projects.set_sharepoint_folder(
            project.id,
            base,
            item.id,
        )

        project.sharepoint_folder_path = base
        project.sharepoint_item_id = item.id

    except Exception:
        # Giống Go: lỗi DB update metadata SharePoint không làm
        # project creation thất bại.
        logger.exception(
            "save project SharePoint folder failed: project=%s",
            project.id,
        )


async def create_project(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()

    workspace, role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot create projects",
            },
        )

    name, key, description = await _read_create_project_body(
        request
    )

    if not name or not key:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name and key are required",
            },
        )

    try:
        project = await self.store.projects.create(
            CreateProjectParams(
                workspace_id=workspace.id,
                name=name,
                key=key,
                description=description,
                created_by=user_id,
            )
        )

    except Exception as exc:
        if _is_unique_violation(exc):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "key_taken",
                    "message": (
                        "project key already used in this workspace"
                    ),
                },
            ) from exc

        raise HTTPException(
            status_code=500,
            detail={
                "error": "create_failed",
                "message": str(exc),
            },
        ) from exc

    # Best-effort giống Go.
    try:
        await self.store.tasks.seed_default_statuses(
            project.id
        )
    except Exception:
        logger.exception(
            "seed workflow statuses failed: project=%s",
            project.id,
        )

    # SharePoint cũng best-effort.
    if self.sharepoint is not None:
        await provision_project_folder(
            self,
            workspace.slug,
            project,
        )

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(project),
    )


async def list_projects(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()

    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    try:
        items = await self.store.projects.list_by_workspace(
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
        "projects": items or [],
    }


async def get_project(
    self: "Handlers",
    project_id: uuid.UUID,
):
    user_id = _current_user_id()

    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    return project