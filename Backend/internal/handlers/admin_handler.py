from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id

if TYPE_CHECKING:
    from .handlers import Handlers


def _attr(obj: Any, *names: str, default=None):
    if obj is None:
        return default

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)

    return default


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


def atoi_or(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_int(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


async def require_admin(self: "Handlers"):
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "requires system admin",
            },
        ) from exc

    if not user.is_system_admin:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "requires system admin",
            },
        )

    return user


async def admin_list_users(
    self: "Handlers",
    request: Request,
) -> dict:
    await self.require_admin()

    query = request.query_params.get("q", "").strip()
    limit = clamp_int(
        atoi_or(request.query_params.get("limit"), 50),
        1,
        200,
    )
    offset = atoi_or(
        request.query_params.get("offset"),
        0,
    )
    if offset < 0:
        offset = 0

    try:
        users = await self.store.users.search(
            query,
            limit,
            offset,
        )
        total = await self.store.users.count_users(query)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {
        "users": users or [],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def admin_toggle_user(
    self: "Handlers",
    user_id: uuid.UUID,
    request: Request,
) -> dict:
    await self.require_admin()

    data = await _read_json_object(
        request,
        {"isSystemAdmin"},
    )

    is_system_admin = data.get("isSystemAdmin", False)
    if not isinstance(is_system_admin, bool):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "isSystemAdmin must be a boolean",
            },
        )

    try:
        await self.store.users.set_system_admin(
            user_id,
            is_system_admin,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "db_error", "message": str(exc)},
        ) from exc

    return {"status": "ok"}


async def admin_list_workspaces(
    self: "Handlers",
):
    await self.require_admin()

    try:
        return await self.store.workspaces.list_all()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "db_error", "message": str(exc)},
        ) from exc


async def admin_create_workspace(
    self: "Handlers",
    request: Request,
):
    actor = await self.require_admin()

    data = await _read_json_object(
        request,
        {"name", "owner_id"},
    )

    name = data.get("name", "")
    raw_owner = data.get("owner_id")

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

    owner_id = actor.id
    if raw_owner not in (None, ""):
        if not isinstance(raw_owner, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": "owner_id must be a UUID",
                },
            )
        try:
            parsed_owner = uuid.UUID(raw_owner)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": "owner_id must be a UUID",
                },
            ) from exc

        if parsed_owner.int != 0:
            owner_id = parsed_owner

    # Exact Go behaviour: lowercase + replace spaces with '-'.
    slug = name.lower().replace(" ", "-")

    try:
        workspace = await self.store.workspaces.create(
            name,
            slug,
            owner_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "db_error", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(workspace),
    )


async def admin_delete_workspace(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    await self.require_admin()

    try:
        await self.store.workspaces.delete(workspace_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "db_error", "message": str(exc)},
        ) from exc

    return {"status": "ok"}


def _azure_cfg(self: "Handlers"):
    return _attr(self.cfg, "azure", "Azure")


def _azure_configured(cfg: Any) -> bool:
    if cfg is None:
        return False

    configured = _attr(cfg, "configured", "Configured")
    if callable(configured):
        try:
            return bool(configured())
        except Exception:
            return False

    tenant_id = _attr(cfg, "tenant_id", "TenantID", default="")
    client_id = _attr(cfg, "client_id", "ClientID", default="")
    client_secret = _attr(cfg, "client_secret", "ClientSecret", default="")
    return bool(tenant_id and client_id and client_secret)


async def admin_sync_azure_users(
    self: "Handlers",
) -> dict:
    await self.require_admin()

    azure_cfg = _azure_cfg(self)
    if not _azure_configured(azure_cfg):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_azure",
                "message": "Azure AD not configured",
            },
        )

    tenant_id = str(
        _attr(azure_cfg, "tenant_id", "TenantID", default="")
    )
    client_id = str(
        _attr(azure_cfg, "client_id", "ClientID", default="")
    )
    client_secret = str(
        _attr(azure_cfg, "client_secret", "ClientSecret", default="")
    )

    token_url = (
        "https://login.microsoftonline.com/"
        + tenant_id
        + "/oauth2/v2.0/token"
    )

    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_response = await client.post(
                token_url,
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if token_response.status_code != 200:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "graph_auth_error",
                        "message": "Failed to get token from Azure",
                    },
                )

            try:
                access_token = token_response.json().get(
                    "access_token",
                    "",
                )
            except ValueError:
                access_token = ""

            if not access_token:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "graph_auth_error",
                        "message": "Failed to get token from Azure",
                    },
                )

            next_link = (
                "https://graph.microsoft.com/v1.0/users"
                "?$select=id,displayName,userPrincipalName,mail"
            )
            count = 0

            admin_emails = (
                _attr(
                    self.cfg,
                    "system_admin_emails",
                    "SystemAdminEmails",
                    default=[],
                )
                or []
            )
            admin_emails_folded = {
                str(email).lower()
                for email in admin_emails
            }

            while next_link:
                response = await client.get(
                    next_link,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                    },
                )

                if response.status_code != 200:
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": "graph_api_error",
                            "message": "Failed to fetch users from Graph",
                        },
                    )

                try:
                    graph_data = response.json()
                except ValueError as exc:
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": "graph_api_error",
                            "message": "Failed to fetch users from Graph",
                        },
                    ) from exc

                for graph_user in graph_data.get("value", []):
                    if not isinstance(graph_user, dict):
                        continue

                    object_id = str(graph_user.get("id") or "")
                    email = str(
                        graph_user.get("mail")
                        or graph_user.get("userPrincipalName")
                        or ""
                    )
                    display_name = str(
                        graph_user.get("displayName")
                        or ""
                    )

                    if not email or not object_id:
                        continue

                    is_admin = (
                        email.lower()
                        in admin_emails_folded
                    )

                    # Match Go: do not fetch profile pictures here.
                    try:
                        await self.store.users.upsert_from_azure(
                            object_id,
                            email,
                            display_name,
                            "",
                            is_admin,
                        )
                    except Exception:
                        continue

                    count += 1

                next_link = str(
                    graph_data.get("@odata.nextLink")
                    or ""
                )

    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "graph_auth_error",
                "message": "Failed to get token from Azure",
            },
        ) from exc

    return {"synced": count}
