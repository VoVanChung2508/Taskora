from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.apikeys import (
    NotFoundError as APIKeyNotFoundError,
    generate_api_key,
)
from ..store.tasks import CreateTaskParams
from ..store.audit import (
    AUDIT_API_KEY_CREATED,
    AUDIT_API_KEY_REVOKED,
)
from .apikey_context import (
    key_from_context,
    reset_api_key,
    set_api_key,
)

if TYPE_CHECKING:
    from .handlers import Handlers


VALID_SCOPES = {"read", "write"}


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



async def list_api_keys(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        keys = await self.store.api_keys.list_by_workspace(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"keys": keys or []}


async def create_api_key(
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
        {"name", "scopes"},
    )

    name = data.get("name", "")
    scopes = data.get("scopes", [])

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    if scopes is None:
        scopes = []

    if not isinstance(scopes, list) or not all(
        isinstance(item, str) for item in scopes
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "scopes must be an array of strings",
            },
        )

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "name is required",
            },
        )

    filtered_scopes = [
        scope
        for scope in scopes
        if scope in VALID_SCOPES
    ]
    if not filtered_scopes:
        filtered_scopes = ["read"]

    try:
        plaintext = generate_api_key()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "keygen_failed", "message": str(exc)},
        ) from exc

    try:
        key = await self.store.api_keys.create(
            workspace.id,
            user_id,
            name,
            filtered_scopes,
            plaintext,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_API_KEY_CREATED,
        key.prefix,
        workspace.id,
        {"name": key.name, "scopes": filtered_scopes},
    )

    # The only response in which the plaintext secret is exposed.
    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "key": key,
                "secret": plaintext,
            }
        ),
    )


async def revoke_api_key(
    self: "Handlers",
    workspace_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        await self.store.api_keys.revoke(
            workspace.id,
            key_id,
        )
    except APIKeyNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "key not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "revoke_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_API_KEY_REVOKED,
        str(key_id),
        workspace.id,
        None,
    )

    return {"ok": True}


async def require_api_key(
    self: "Handlers",
    request: Request,
) -> AsyncIterator[None]:
    """
    FastAPI dependency equivalent of the Go middleware.

    X-API-Key takes precedence. Otherwise accepts Authorization: Bearer flw_...
    and exposes the resolved key through a ContextVar for downstream handlers.
    """
    raw = request.headers.get("X-API-Key", "")

    if not raw:
        authorization = request.headers.get("Authorization", "")
        bearer = "Bearer "
        if authorization.startswith(bearer):
            raw = authorization[len(bearer):]

    try:
        resolved = await self.store.api_keys.resolve(raw)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "API key không hợp lệ hoặc đã bị thu hồi",
            },
        ) from exc

    token = set_api_key(resolved)
    try:
        yield None
    finally:
        reset_api_key(token)


def _require_resolved_key():
    key, ok = key_from_context()
    if not ok or key is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "",
            },
        )
    return key


async def api_projects(
    self: "Handlers",
) -> dict:
    key = _require_resolved_key()

    try:
        projects = await self.store.projects.list_by_workspace(
            key.workspace_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"projects": projects or []}


async def api_tasks(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    key = _require_resolved_key()

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

    if project.workspace_id != key.workspace_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "project not found",
            },
        )

    try:
        tasks = await self.store.tasks.list_by_project_enriched(
            project.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"tasks": tasks or []}


async def api_create_task(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    key = _require_resolved_key()

    if not key.has_scope("write"):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "message": "key này chỉ có quyền read",
            },
        )

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

    if project.workspace_id != key.workspace_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "project not found",
            },
        )

    data = await _read_json_object(
        request,
        {"title", "status", "priority"},
    )

    title = data.get("title", "")
    status = data.get("status", "")
    priority = data.get("priority", "")

    for field_name, value in (
        ("title", title),
        ("status", status),
        ("priority", priority),
    ):
        if not isinstance(value, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": f"{field_name} must be a string",
                },
            )

    title = title.strip()
    if not title:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "title is required",
            },
        )

    try:
        task = await self.store.tasks.create(
            CreateTaskParams(
                project_id=project.id,
                title=title,
                reporter_id=uuid.UUID(int=0),
                status=status,
                priority=priority,
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    await self.emit(
        project.id,
        uuid.UUID(int=0),
        "task.created",
        {
            "taskId": task.id,
            "via": "api",
        },
    )

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(task),
    )
