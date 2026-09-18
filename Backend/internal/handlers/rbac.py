from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from .handlers import Handlers


def can_manage_workspace(role: str) -> bool:
    """Owner và admin có quyền quản lý workspace."""
    return role in {"owner", "admin"}


async def require_workspace_member(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
):
    """
    Kiểm tra user có thuộc workspace hay không.

    Tương đương requireWorkspaceMember trong Go.
    Trả 404 thay vì 403 để không làm lộ workspace tồn tại.
    """
    try:
        role = await self.store.workspaces.role_for_user(
            workspace_id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "workspace not found",
            },
        ) from exc

    try:
        workspace = await self.store.workspaces.get_by_id(
            workspace_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "workspace not found",
            },
        ) from exc

    return workspace, role


async def require_project_access(
    self: "Handlers",
    project_id: uuid.UUID,
    user_id: uuid.UUID,
):
    """
    Kiểm tra project tồn tại và user có quyền truy cập workspace chứa project.

    Tương đương requireProjectAccess trong Go.
    """
    try:
        project = await self.store.projects.get_by_id(project_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "project not found",
            },
        ) from exc

    try:
        role = await self.store.workspaces.role_for_user(
            project.workspace_id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "project not found",
            },
        ) from exc

    return project, role


async def require_task_access(
    self: "Handlers",
    task_id: uuid.UUID,
    user_id: uuid.UUID,
):
    """
    Kiểm tra task tồn tại và user có quyền truy cập workspace chứa task.

    Tương đương requireTaskAccess trong Go.
    Trả 404 nếu task/project/workspace không truy cập được để tránh lộ dữ liệu.
    """
    try:
        task = await self.store.tasks.get_by_id(task_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "task not found",
            },
        ) from exc

    try:
        project = await self.store.projects.get_by_id(
            task.project_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "task not found",
            },
        ) from exc

    try:
        role = await self.store.workspaces.role_for_user(
            project.workspace_id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "task not found",
            },
        ) from exc

    return task, role