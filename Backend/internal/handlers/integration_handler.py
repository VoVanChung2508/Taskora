from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.integrations import NotFoundError as IntegrationNotFoundError
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


logger = logging.getLogger(__name__)
VALID_PROVIDERS = {"slack", "teams"}


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


def event_headline(
    event_type: str,
    project_name: str,
    payload: dict[str, Any],
) -> str:
    if event_type == "task.created":
        return f"🆕 Công việc mới trong *{project_name}*"
    if event_type == "task.status_changed":
        return (
            f"🔄 *{project_name}*: trạng thái "
            f"{payload.get('from')} → {payload.get('to')}"
        )
    if event_type == "task.commented":
        return f"💬 Bình luận mới trong *{project_name}*"
    if event_type == "task.deleted":
        return f"🗑️ Một công việc trong *{project_name}* đã bị xoá"
    if event_type == "chat.message":
        return f"📨 Tin nhắn mới trong *{project_name}*"
    return f"Flowie · {event_type} ({project_name})"


def chat_payload(provider: str, text: str) -> dict[str, Any]:
    if provider == "teams":
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": "Flowie",
            "text": text,
        }
    return {"text": text}


async def _dispatch_integrations_worker(
    self: "Handlers",
    project_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    try:
        integrations = await self.store.integrations.active_for_event(
            project_id,
            event_type,
        )
    except Exception:
        logger.exception(
            "integration lookup failed project=%s event=%s",
            project_id,
            event_type,
        )
        return

    if not integrations:
        return

    project_name = ""
    try:
        project = await self.store.projects.get_by_id(project_id)
        project_name = project.name
    except Exception:
        pass

    text = event_headline(event_type, project_name, payload)

    async with httpx.AsyncClient(timeout=10.0) as client:
        for integration in integrations:
            try:
                response = await client.post(
                    integration.webhook_url,
                    json=chat_payload(integration.provider, text),
                    headers={"Content-Type": "application/json"},
                )
                error_message = (
                    "non-2xx response"
                    if response.status_code >= 300
                    else ""
                )
                await self.store.integrations.record_delivery(
                    integration.id,
                    response.status_code,
                    error_message,
                )
            except Exception as exc:
                try:
                    await self.store.integrations.record_delivery(
                        integration.id,
                        0,
                        str(exc),
                    )
                except Exception:
                    pass


def dispatch_integrations(
    self: "Handlers",
    project_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """
    Fire-and-forget delivery. Equivalent to the Go goroutine: outbound chat
    endpoints never delay the request that generated the event.
    """
    asyncio.create_task(
        _dispatch_integrations_worker(
            self,
            project_id,
            event_type,
            payload,
        )
    )


async def list_integrations(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
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
        integrations = await self.store.integrations.list_by_project(
            project.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"integrations": integrations or []}


async def create_integration(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
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

    data = await _read_json_object(
        request,
        {"provider", "webhookUrl", "events"},
    )

    provider = data.get("provider", "")
    webhook_url = data.get("webhookUrl", "")
    events = data.get("events", [])

    if not isinstance(provider, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "provider must be a string",
            },
        )

    if not isinstance(webhook_url, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "webhookUrl must be a string",
            },
        )

    if events is None:
        events = []

    if not isinstance(events, list) or not all(
        isinstance(item, str) for item in events
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "events must be an array of strings",
            },
        )

    provider = provider.strip().lower()
    if provider not in VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "provider must be slack or teams",
            },
        )

    webhook_url = webhook_url.strip()
    parsed = urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "webhookUrl phải là URL https hợp lệ",
            },
        )

    try:
        integration = await self.store.integrations.create(
            project.id,
            provider,
            webhook_url,
            events,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(integration),
    )


async def delete_integration(
    self: "Handlers",
    project_id: uuid.UUID,
    integration_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
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
        await self.store.integrations.delete(
            project.id,
            integration_id,
        )
    except IntegrationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "integration not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
