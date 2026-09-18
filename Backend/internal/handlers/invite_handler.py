from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.audit import AUDIT_MEMBER_ADDED
from ..store.invites import (
    InviteEmailMismatchError,
    NotFoundError as InviteNotFoundError,
    generate_invite_token,
)

if TYPE_CHECKING:
    from .handlers import Handlers


INVITE_TTL = timedelta(days=14)
VALID_INVITE_ROLES = {"admin", "billing", "member", "guest"}


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


async def list_invites(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        invites = await self.store.invites.list_pending(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"invites": invites or []}


async def create_invite(
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
        {"email", "role"},
    )

    email = data.get("email", "")
    role = data.get("role", "")

    if not isinstance(email, str) or not isinstance(role, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "email and role must be strings",
            },
        )

    email = email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "email không hợp lệ",
            },
        )

    role = role.strip() or "member"
    if role not in VALID_INVITE_ROLES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "vai trò không hợp lệ",
            },
        )

    try:
        token = generate_invite_token()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "token_failed", "message": str(exc)},
        ) from exc

    try:
        invite = await self.store.invites.create(
            workspace.id,
            email,
            role,
            user_id,
            token,
            INVITE_TTL,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_MEMBER_ADDED,
        email,
        workspace.id,
        {"invited": True, "role": role},
    )

    frontend_url = (
        getattr(self.cfg, "frontend_url", None)
        or getattr(self.cfg, "FrontendURL", "")
        or ""
    ).rstrip("/")

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "invite": invite,
                "inviteUrl": frontend_url + "/invite/" + token,
            }
        ),
    )


async def revoke_invite(
    self: "Handlers",
    workspace_id: uuid.UUID,
    invite_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        await self.store.invites.revoke(
            workspace.id,
            invite_id,
        )
    except InviteNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "invite không tồn tại",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "revoke_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def accept_invite(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "user not found"},
        ) from exc

    data = await _read_json_object(request, {"token"})
    token = data.get("token", "")
    if not isinstance(token, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "token must be a string",
            },
        )

    try:
        invite = await self.store.invites.accept(
            token.strip(),
            user_id,
            user.email,
        )
    except InviteEmailMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "email_mismatch",
                "message": "lời mời này dành cho một địa chỉ email khác",
            },
        ) from exc
    except InviteNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "invalid_invite",
                "message": "lời mời không hợp lệ hoặc đã hết hạn",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "accept_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_MEMBER_ADDED,
        user.email,
        invite.workspace_id,
        {"via": "invite", "role": invite.role},
    )

    return {"workspaceId": invite.workspace_id}
