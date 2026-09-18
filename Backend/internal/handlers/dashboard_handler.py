from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.dashboards import NotFoundError as DashboardNotFoundError
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


VALID_WIDGET_TYPES = {
    "kpi",
    "status_donut",
    "priority_bar",
    "trend",
    "project_table",
    "velocity",
}


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
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


async def list_dashboards(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace, _role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    try:
        dashboards = await self.store.dashboards.list_for_user(
            workspace.id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"dashboards": dashboards or []}


async def create_dashboard(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    workspace, role = await self.require_workspace_member(
        workspace_id,
        user_id,
    )

    data = await _read_json_object(
        request,
        {"name", "shared"},
    )

    name = data.get("name", "")
    shared = data.get("shared", False)

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "name must be a string"},
        )
    if not isinstance(shared, bool):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "shared must be a boolean"},
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "name is required"},
        )

    owner: uuid.UUID | None = user_id
    if shared:
        if not can_manage_workspace(role):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "forbidden",
                    "message": "chỉ owner/admin mới tạo dashboard dùng chung",
                },
            )
        owner = None

    try:
        dashboard = await self.store.dashboards.create_dashboard(
            workspace.id,
            owner,
            name,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(dashboard),
    )


async def require_dashboard_access(
    self: "Handlers",
    dashboard_id: uuid.UUID,
    user_id: uuid.UUID,
) -> uuid.UUID:
    try:
        workspace_id = await self.store.dashboards.dashboard_workspace(
            dashboard_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "dashboard not found"},
        ) from exc

    try:
        await self.store.workspaces.role_for_user(
            workspace_id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "dashboard not found"},
        ) from exc

    return dashboard_id


async def delete_dashboard(
    self: "Handlers",
    dashboard_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    dashboard_id = await self.require_dashboard_access(
        dashboard_id,
        user_id,
    )

    try:
        workspace_id = await self.store.dashboards.dashboard_workspace(
            dashboard_id
        )
        await self.store.dashboards.delete_dashboard(
            workspace_id,
            dashboard_id,
        )
    except DashboardNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "dashboard not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def add_widget(
    self: "Handlers",
    dashboard_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    dashboard_id = await self.require_dashboard_access(
        dashboard_id,
        user_id,
    )

    data = await _read_json_object(
        request,
        {"type", "title", "config", "width"},
    )

    widget_type = data.get("type", "")
    title = data.get("title", "")
    config = data.get("config")
    width = data.get("width", 0)

    if not isinstance(widget_type, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "type must be a string"},
        )
    if not isinstance(title, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "title must be a string"},
        )
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "config must be an object"},
        )
    if isinstance(width, bool) or not isinstance(width, int):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "width must be an integer"},
        )

    if widget_type not in VALID_WIDGET_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "unknown widget type: " + widget_type,
            },
        )

    if width < 1 or width > 3:
        width = 1

    try:
        widget = await self.store.dashboards.add_widget(
            dashboard_id,
            widget_type,
            title.strip(),
            config,
            width,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(widget),
    )


async def delete_widget(
    self: "Handlers",
    dashboard_id: uuid.UUID,
    widget_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    dashboard_id = await self.require_dashboard_access(
        dashboard_id,
        user_id,
    )

    try:
        await self.store.dashboards.delete_widget(
            dashboard_id,
            widget_id,
        )
    except DashboardNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "widget not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
