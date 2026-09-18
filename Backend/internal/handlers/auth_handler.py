from __future__ import annotations

import base64
import hashlib
import inspect
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth.session import get_user_id
from ..store.sessions import mark_current
from ..store.audit import (
    AUDIT_LOGIN,
    AUDIT_SESSION_REVOKED,
)
from ..util.slug import slugify

if TYPE_CHECKING:
    from .handlers import Handlers


SESSION_COOKIE = "flowie_session"
FLOW_COOKIE_TTL = 10 * 60


def _cfg(self: "Handlers", snake: str, camel: str | None = None, default=None):
    if hasattr(self.cfg, snake):
        return getattr(self.cfg, snake)
    if camel and hasattr(self.cfg, camel):
        return getattr(self.cfg, camel)
    return default


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )
    return user_id


def random_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_from_request(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        return token
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        return authz[len("Bearer "):]
    return ""


def _set_flow_cookie(self: "Handlers", response, name: str, value: str) -> None:
    secure = _cfg(self, "env", "Env", "development") != "development"
    response.set_cookie(
        key=name,
        value=value,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=FLOW_COOKIE_TTL,
    )


def _set_session_cookie(self: "Handlers", response, token: str) -> None:
    secure = _cfg(self, "env", "Env", "development") != "development"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def _clear_session_cookie(self: "Handlers", response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        httponly=True,
    )


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _issue_session(self: "Handlers", user, pending: bool = False) -> str:
    method_name = "issue_pending" if pending else "issue"
    method = getattr(self.sessions, method_name, None)
    if method is None:
        raise RuntimeError(f"SessionManager.{method_name} is missing")
    return await _maybe_await(method(user.id, user.email, user.display_name))


async def _verify_session(self: "Handlers", token: str):
    method = getattr(self.sessions, "verify", None)
    if method is None:
        raise RuntimeError("SessionManager.verify is missing")
    return await _maybe_await(method(token))


def _claim(claims, *names, default=None):
    for name in names:
        if isinstance(claims, dict) and name in claims:
            return claims[name]
        if hasattr(claims, name):
            return getattr(claims, name)
    return default


def _claim_expiry(claims) -> datetime | None:
    value = _claim(claims, "expires_at", "ExpiresAt", "exp")
    if value is None:
        return None
    if hasattr(value, "time"):
        value = value.time
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


async def _token_expiry(self: "Handlers", token: str) -> datetime:
    try:
        claims = await _verify_session(self, token)
        expiry = _claim_expiry(claims)
        if expiry is not None:
            return expiry
    except Exception:
        pass
    return datetime.now(timezone.utc) + timedelta(days=7)


async def record_session(
    self: "Handlers",
    request: Request,
    user_id: uuid.UUID,
    token: str,
) -> None:
    ip = request.headers.get("X-Forwarded-For", "")
    if not ip:
        ip = request.client.host if request.client else ""

    device = request.headers.get("User-Agent", "")
    if len(device) > 400:
        device = device[:400]

    try:
        await self.store.sessions.create(
            user_id,
            hash_token(token),
            device,
            ip,
            await _token_expiry(self, token),
        )
    except Exception:
        # Session tracking must not make login fail.
        pass


def development_workspace_name(user) -> str:
    if user is None:
        return "Development Workspace"
    name = (getattr(user, "display_name", "") or "").strip()
    if name:
        return f"{name} Workspace"
    email = (getattr(user, "email", "") or "").strip()
    if email:
        return f"{email} Workspace"
    return "Development Workspace"


async def ensure_development_workspace(self: "Handlers", user):
    if user is None:
        return None
    items = await self.store.workspaces.list_for_user(user.id)
    if items:
        return items[0]
    name = development_workspace_name(user)
    return await self.store.workspaces.create(
        name,
        slugify(name),
        user.id,
    )


def _azure_method(azure, *names):
    for name in names:
        method = getattr(azure, name, None)
        if method is not None:
            return method
    return None


async def _azure_auth_url(azure, state: str, nonce: str) -> str:
    method = _azure_method(azure, "auth_code_url", "AuthCodeURL")
    if method is None:
        raise RuntimeError("AzureProvider auth-code URL method is missing")
    return await _maybe_await(method(state, nonce))


async def _azure_exchange(azure, code: str, nonce: str):
    method = _azure_method(azure, "exchange", "Exchange")
    if method is None:
        raise RuntimeError("AzureProvider exchange method is missing")
    # Most Python ports omit context; support both signatures.
    try:
        return await _maybe_await(method(code, nonce))
    except TypeError:
        return await _maybe_await(method(None, code, nonce))


def _claims_value(claims, method_names: tuple[str, ...], attr_names: tuple[str, ...]) -> str:
    for name in method_names:
        fn = getattr(claims, name, None)
        if callable(fn):
            value = fn()
            if value:
                return str(value)
    for name in attr_names:
        if isinstance(claims, dict) and claims.get(name):
            return str(claims[name])
        value = getattr(claims, name, None)
        if value:
            return str(value)
    return ""


async def azure_login(self: "Handlers"):
    if self.azure is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "sso_disabled", "message": "Azure AD is not configured"},
        )

    state = random_token()
    nonce = random_token()

    try:
        url = await _azure_auth_url(self.azure, state, nonce)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "sso_url_failed", "message": str(exc)},
        ) from exc

    response = RedirectResponse(url=url, status_code=302)
    _set_flow_cookie(self, response, "oauth_state", state)
    _set_flow_cookie(self, response, "oauth_nonce", nonce)
    return response


async def azure_callback(self: "Handlers", request: Request):
    if self.azure is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "sso_disabled", "message": "Azure AD is not configured"},
        )

    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "sso_error",
                "message": request.query_params.get("error_description", ""),
            },
        )

    state_cookie = request.cookies.get("oauth_state", "")
    if not state_cookie or state_cookie != request.query_params.get("state", ""):
        raise HTTPException(
            status_code=400,
            detail={"error": "state_mismatch", "message": "invalid oauth state"},
        )

    nonce = request.cookies.get("oauth_nonce", "")
    if not nonce:
        raise HTTPException(
            status_code=400,
            detail={"error": "nonce_missing", "message": "missing oauth nonce"},
        )

    try:
        claims = await _azure_exchange(
            self.azure,
            request.query_params.get("code", ""),
            nonce,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "sso_exchange_failed", "message": str(exc)},
        ) from exc

    email = _claims_value(
        claims,
        ("resolved_email", "ResolvedEmail"),
        ("email", "preferred_username"),
    )
    oid = _claims_value(
        claims,
        ("resolved_oid", "ResolvedOID"),
        ("oid", "object_id", "sub"),
    )
    name = _claims_value(claims, (), ("name", "display_name"))
    picture = _claims_value(claims, (), ("picture", "avatar_url"))

    if not email or not oid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "insufficient_claims",
                "message": "missing email or oid in token",
            },
        )

    admin_emails = _cfg(
        self,
        "system_admin_emails",
        "SystemAdminEmails",
        [],
    ) or []
    is_admin = any(str(item).lower() == email.lower() for item in admin_emails)

    try:
        user = await self.store.users.upsert_from_azure(
            oid,
            email,
            name,
            picture,
            is_admin,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "provision_failed", "message": str(exc)},
        ) from exc

    frontend_url = str(_cfg(self, "frontend_url", "FrontendURL", "") or "").rstrip("/")

    try:
        state = await self.store.users.two_factor(user.id)
    except Exception:
        state = None

    if state is not None and state.enabled:
        try:
            token = await _issue_session(self, user, pending=True)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "session_failed", "message": str(exc)},
            ) from exc

        response = RedirectResponse(
            url=frontend_url + "/login/2fa",
            status_code=302,
        )
        _set_session_cookie(self, response, token)
    else:
        try:
            token = await _issue_session(self, user)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "session_failed", "message": str(exc)},
            ) from exc

        response = RedirectResponse(url=frontend_url or "/", status_code=302)
        _set_session_cookie(self, response, token)
        await record_session(self, request, user.id, token)
        await self.audit_for(
            request,
            user.id,
            user.email,
            AUDIT_LOGIN,
            "azure_ad",
            None,
        )

    _set_flow_cookie(self, response, "oauth_state", "")
    _set_flow_cookie(self, response, "oauth_nonce", "")
    return response


async def dev_login(self: "Handlers", request: Request):
    if _cfg(self, "env", "Env", "development") != "development":
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": ""},
        )

    email = request.query_params.get("email", "") or "dev@flowie.local"
    name = request.query_params.get("name", "") or "Dev User"

    admin_emails = _cfg(
        self,
        "system_admin_emails",
        "SystemAdminEmails",
        [],
    ) or []
    is_admin = any(str(item).lower() == email.lower() for item in admin_emails)

    try:
        user = await self.store.users.upsert_from_azure(
            "dev|" + email,
            email,
            name,
            "",
            is_admin,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "provision_failed", "message": str(exc)},
        ) from exc

    try:
        state = await self.store.users.two_factor(user.id)
    except Exception:
        state = None

    redirect_requested = bool(request.query_params.get("redirect"))
    frontend_url = str(_cfg(self, "frontend_url", "FrontendURL", "") or "").rstrip("/")

    if state is not None and state.enabled:
        try:
            token = await _issue_session(self, user, pending=True)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "session_failed", "message": str(exc)},
            ) from exc

        if redirect_requested:
            response = RedirectResponse(
                url=frontend_url + "/login/2fa",
                status_code=302,
            )
        else:
            response = JSONResponse({"mfaRequired": True})

        _set_session_cookie(self, response, token)
        return response

    try:
        await ensure_development_workspace(self, user)
    except Exception:
        pass

    try:
        token = await _issue_session(self, user)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "session_failed", "message": str(exc)},
        ) from exc

    if redirect_requested:
        response = RedirectResponse(url=frontend_url or "/", status_code=302)
    else:
        response = JSONResponse(content=jsonable_encoder(user))

    _set_session_cookie(self, response, token)
    await record_session(self, request, user.id, token)
    return response


async def logout(self: "Handlers", request: Request):
    token = token_from_request(request)
    if token:
        try:
            await self.store.sessions.revoke_by_token(hash_token(token))
        except Exception:
            pass

    response = JSONResponse({"status": "logged_out"})
    _clear_session_cookie(self, response)
    return response


async def list_sessions(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()

    try:
        sessions = await self.store.sessions.list_for_user(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    token = token_from_request(request)
    if token:
        try:
            current_id = await self.store.sessions.id_by_token(
                hash_token(token)
            )
            mark_current(sessions, current_id)
        except Exception:
            pass

    return {"sessions": sessions or []}


async def revoke_session(
    self: "Handlers",
    session_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()

    await self.audit(
        request,
        AUDIT_SESSION_REVOKED,
        str(session_id),
        None,
        None,
    )

    try:
        await self.store.sessions.revoke(
            user_id,
            session_id,
        )
    except Exception as exc:
        # The store's revoke only raises NotFoundError for a missing/foreign row.
        if exc.__class__.__name__ == "NotFoundError":
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": "session not found"},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"error": "revoke_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def dev_make_admin(self: "Handlers") -> dict:
    # Security fix over the Go source: the Go handler was registered
    # unconditionally even though its comment says DEV ONLY.
    if _cfg(self, "env", "Env", "development") != "development":
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": ""},
        )

    user_id = _current_user_id()
    try:
        await self.store.users.set_system_admin(user_id, True)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "db_error", "message": str(exc)},
        ) from exc
    return {"message": "You are now an admin"}


async def me(self: "Handlers"):
    user_id = _current_user_id()
    try:
        return await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "user not found"},
        ) from exc
