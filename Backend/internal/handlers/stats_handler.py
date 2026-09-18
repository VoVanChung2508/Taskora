from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from ..auth.session import get_user_id
from ..store.overview import parse_trend_range

if TYPE_CHECKING:
    from .handlers import Handlers


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )
    return user_id


async def project_stats(
    self: "Handlers",
    project_id: uuid.UUID,
):
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        return await self.store.tasks.project_stats(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "stats_failed", "message": str(exc)},
        ) from exc


async def workspace_overview(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    trend_range = parse_trend_range(
        request.query_params.get("range", "")
    )

    try:
        return await self.store.tasks.workspace_overview(
            workspace.id,
            trend_range,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "overview_failed", "message": str(exc)},
        ) from exc


async def project_overview(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    trend_range = parse_trend_range(
        request.query_params.get("range", "")
    )

    try:
        return await self.store.tasks.project_overview(
            project.id,
            trend_range,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "overview_failed", "message": str(exc)},
        ) from exc


async def project_critical_path(
    self: "Handlers",
    project_id: uuid.UUID,
):
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        return await self.store.tasks.critical_path(project.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "cpm_failed", "message": str(exc)},
        ) from exc


async def dashboard(
    self: "Handlers",
    request: Request,
):
    user_id = _current_user_id()
    raw_workspace_id = request.query_params.get(
        "workspace_id",
        "",
    )

    try:
        if raw_workspace_id:
            try:
                workspace_id = uuid.UUID(raw_workspace_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "invalid_workspace",
                        "message": "Invalid workspace ID",
                    },
                ) from exc

            return await self.store.tasks.workspace_dashboard_stats(
                user_id,
                workspace_id,
            )

        return await self.store.tasks.dashboard_stats(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "stats_failed", "message": str(exc)},
        ) from exc
