from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.webhooks import NotFoundError as WebhookNotFoundError
from .rbac import can_manage_workspace

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


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def _public_webhook(webhook) -> dict[str, Any]:
    """
    The Go domain model never serializes the secret. The migrated Python
    dataclass contains it, so handlers must explicitly remove it.
    """
    return {
        "id": webhook.id,
        "projectId": webhook.project_id,
        "url": webhook.url,
        "events": webhook.events,
        "active": webhook.active,
        "lastStatus": webhook.last_status,
        "lastError": webhook.last_error,
        "lastSentAt": webhook.last_sent_at,
        "createdAt": webhook.created_at,
        "hasSecret": bool(webhook.secret),
    }


async def _dispatch_webhooks_worker(
    self: "Handlers",
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    try:
        hooks = await self.store.webhooks.active_for_event(
            project_id,
            event_type,
        )
    except Exception:
        logger.exception(
            "webhook lookup failed project=%s event=%s",
            project_id,
            event_type,
        )
        return

    if not hooks:
        return

    body_obj = {
        "type": event_type,
        "projectId": project_id,
        "actorId": actor_id,
        "payload": payload,
        "sentAt": datetime.now(timezone.utc),
    }
    body = json.dumps(
        jsonable_encoder(body_obj),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    async with httpx.AsyncClient(timeout=10.0) as client:
        for webhook in hooks:
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Flowie-Webhook/1",
                "X-Flowie-Event": event_type,
            }

            if webhook.secret:
                headers["X-Flowie-Signature"] = (
                    "sha256=" + sign_payload(webhook.secret, body)
                )

            try:
                response = await client.post(
                    webhook.url,
                    content=body,
                    headers=headers,
                )
                error_message = (
                    "non-2xx response"
                    if response.status_code >= 300
                    else ""
                )
                await self.store.webhooks.record_delivery(
                    webhook.id,
                    response.status_code,
                    error_message,
                )
            except Exception as exc:
                try:
                    await self.store.webhooks.record_delivery(
                        webhook.id,
                        0,
                        str(exc),
                    )
                except Exception:
                    pass
                logger.warning(
                    "webhook delivery failed url=%s error=%s",
                    webhook.url,
                    exc,
                )


def dispatch_webhooks(
    self: "Handlers",
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    asyncio.create_task(
        _dispatch_webhooks_worker(
            self,
            project_id,
            actor_id,
            event_type,
            payload,
        )
    )


async def emit(
    self: "Handlers",
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """
    Fan out an event.

    Live/SSE publishing is intentionally invoked only when the realtime
    publish helper is already wired. Webhook/integration delivery is always
    fire-and-forget, matching the original Go goroutines.
    """
    publish = getattr(self, "publish", None)
    if publish is not None:
        try:
            result = publish(
                project_id,
                actor_id,
                event_type,
                payload,
            )
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.exception(
                "realtime publish failed project=%s event=%s",
                project_id,
                event_type,
            )

    self.dispatch_webhooks(
        project_id,
        actor_id,
        event_type,
        payload,
    )
    self.dispatch_integrations(
        project_id,
        event_type,
        payload,
    )


async def list_webhooks(
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
        hooks = await self.store.webhooks.list_by_project(
            project.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {
        "webhooks": [
            _public_webhook(item)
            for item in (hooks or [])
        ]
    }


async def create_webhook(
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
        {"url", "events", "secret"},
    )

    url = data.get("url", "")
    events = data.get("events", [])
    secret = data.get("secret", "")

    if not isinstance(url, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "url must be a string"},
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

    if not isinstance(secret, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "secret must be a string",
            },
        )

    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "url must be a valid http(s) URL",
            },
        )

    try:
        hook = await self.store.webhooks.create(
            project.id,
            url,
            events,
            secret.strip(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(_public_webhook(hook)),
    )


async def delete_webhook(
    self: "Handlers",
    project_id: uuid.UUID,
    webhook_id: uuid.UUID,
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
        await self.store.webhooks.delete(
            project.id,
            webhook_id,
        )
    except WebhookNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "webhook not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
