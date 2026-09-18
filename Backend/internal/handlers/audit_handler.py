from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from ..auth.session import get_user_id
from ..store.audit import AuditEntryInput

if TYPE_CHECKING:
    from .handlers import Handlers


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded

    if request.client is None:
        return ""

    if request.client.port is None:
        return request.client.host

    return f"{request.client.host}:{request.client.port}"


async def audit(
    self: "Handlers",
    request: Request,
    action: str,
    target: str,
    workspace_id: uuid.UUID | None,
    meta: dict[str, Any] | None,
) -> None:
    entry = AuditEntryInput(
        action=action,
        target=target,
        workspace_id=workspace_id,
        ip=client_ip(request),
        meta=meta,
    )

    user_id, ok = get_user_id()
    if ok and user_id is not None:
        entry.actor_id = user_id
        try:
            user = await self.store.users.get_by_id(user_id)
            entry.actor_email = user.email
        except Exception:
            pass

    # AuditStore.record itself is best-effort and swallows DB failures.
    await self.store.audit.record(entry)


async def audit_for(
    self: "Handlers",
    request: Request,
    user_id: uuid.UUID,
    email: str,
    action: str,
    target: str,
    meta: dict[str, Any] | None,
) -> None:
    await self.store.audit.record(
        AuditEntryInput(
            action=action,
            actor_id=user_id,
            actor_email=email,
            target=target,
            ip=client_ip(request),
            meta=meta,
        )
    )


def _limit_from_request(request: Request) -> int:
    raw = request.query_params.get("limit", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def list_audit_log(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )

    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        entries = await self.store.audit.list(
            workspace.id,
            _limit_from_request(request),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"entries": entries or []}


async def admin_audit_log(
    self: "Handlers",
    request: Request,
) -> dict:
    await self.require_admin()

    try:
        entries = await self.store.audit.list(
            None,
            _limit_from_request(request),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"entries": entries or []}
