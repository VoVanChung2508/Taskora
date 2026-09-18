from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from .handlers import Handlers


def default_role_grants(role: str, permission: str) -> bool:
    """
    Built-in workspace role baseline.

    A custom role, when assigned, replaces this baseline. Owner/admin are
    handled in has_permission() and always pass.
    """
    if role == "guest":
        return permission == "comment.create"

    if role == "billing":
        return permission == "budget.view"

    if role == "member":
        return permission in {
            "task.create",
            "task.edit",
            "comment.create",
            "worklog.log",
            "sprint.manage",
            "budget.view",
        }

    return False


async def has_permission(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    permission: str,
) -> bool:
    """
    Resolve a fine-grained permission.

    Owner/admin always pass. If the member has a custom role, that explicit
    permission list is authoritative and replaces the built-in role defaults.
    Lookup failures fail closed.
    """
    if role in {"owner", "admin"}:
        return True

    try:
        permissions = await self.store.workspaces.permissions_for_user(
            workspace_id,
            user_id,
        )
    except Exception:
        return False

    if permissions is not None:
        return permission in permissions

    return default_role_grants(role, permission)


async def require_permission(
    self: "Handlers",
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    permission: str,
) -> bool:
    """
    Raise 403 when the caller lacks the requested permission.
    """
    if await has_permission(
        self,
        workspace_id,
        user_id,
        role,
        permission,
    ):
        return True

    raise HTTPException(
        status_code=403,
        detail={
            "error": "insufficient_permission",
            "message": f"bạn không có quyền {permission}",
        },
    )
