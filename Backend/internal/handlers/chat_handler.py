from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from .rbac import can_manage_workspace
from .task_handler import parse_mentions

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


async def require_channel_access(
    self: "Handlers",
    channel_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, str]:
    try:
        project_id = await self.store.chat.channel_project(
            channel_id
        )
        project = await self.store.projects.get_by_id(
            project_id
        )
        role = await self.store.workspaces.role_for_user(
            project.workspace_id,
            user_id,
        )
    except Exception as exc:
        # Deliberately hide channel existence from non-members.
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "channel not found"},
        ) from exc

    return channel_id, role


async def list_channels(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        channels = await self.store.chat.list_channels(
            project.id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"channels": channels or []}


async def create_channel(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot create channels",
            },
        )

    data = await _read_json_object(request, {"name"})
    name = data.get("name", "")

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "name must be a string"},
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "name is required"},
        )

    try:
        channel = await self.store.chat.create_channel(
            project.id,
            name,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(channel),
    )


async def delete_channel(
    self: "Handlers",
    channel_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    channel_id, role = await self.require_channel_access(
        channel_id,
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

    try:
        await self.store.chat.delete_channel(channel_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def list_messages(
    self: "Handlers",
    channel_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    channel_id, _role = await self.require_channel_access(
        channel_id,
        user_id,
    )

    raw_limit = request.query_params.get("limit", "")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 0

    try:
        messages = await self.store.chat.list_messages(
            channel_id,
            limit,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.chat.mark_read(
            channel_id,
            user_id,
        )
    except Exception:
        pass

    return {"messages": messages or []}


async def notify_chat_mentions(
    self: "Handlers",
    channel_id: uuid.UUID,
    body: str,
    author_id: uuid.UUID,
) -> None:
    tokens = parse_mentions(body)
    if not tokens:
        return

    try:
        project_id = await self.store.chat.channel_project(
            channel_id
        )
        project = await self.store.projects.get_by_id(
            project_id
        )
        members = await self.store.workspaces.list_members(
            project.workspace_id
        )
    except Exception:
        return

    for member in members:
        if member.user_id == author_id:
            continue

        email = (member.email or "").lower()
        local = email.split("@", 1)[0] if "@" in email else email

        display_name = member.display_name or ""
        parts = display_name.split()
        first = parts[0].lower() if parts else ""

        if (
            email in tokens
            or local in tokens
            or (first and first in tokens)
        ):
            try:
                await self.store.notifications.create(
                    member.user_id,
                    "mentioned",
                    "Bạn được nhắc đến trong chat",
                    project.name,
                    None,
                    f"/projects/{project.id}/chat",
                )
            except Exception:
                pass


async def post_message(
    self: "Handlers",
    channel_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    channel_id, role = await self.require_channel_access(
        channel_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot post messages",
            },
        )

    data = await _read_json_object(request, {"body"})
    body = data.get("body", "")

    if not isinstance(body, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "body must be a string"},
        )

    body = body.strip()
    if not body:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "body is required"},
        )

    try:
        message = await self.store.chat.post_message(
            channel_id,
            user_id,
            body,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "post_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.chat.mark_read(
            channel_id,
            user_id,
        )
    except Exception:
        pass

    await self.notify_chat_mentions(
        channel_id,
        body,
        user_id,
    )

    try:
        project_id = await self.store.chat.channel_project(
            channel_id
        )
        await self.publish(
            project_id,
            user_id,
            "chat.message",
            {
                "channelId": channel_id,
                "messageId": message.id,
            },
        )
    except Exception:
        pass

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(message),
    )


async def mark_channel_read(
    self: "Handlers",
    channel_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    channel_id, _role = await self.require_channel_access(
        channel_id,
        user_id,
    )

    try:
        await self.store.chat.mark_read(
            channel_id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "update_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
