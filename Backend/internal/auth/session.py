"""Session manager: issues and verifies signed session tokens (JWT) and
manages the session cookie.

Python port of the Go `auth.SessionManager`.

Requires:
    pip install PyJWT fastapi

This module implements:
- token_from_request
- hash_token
- SessionManager
- FastAPI require_auth dependency
- get_user_id
"""

from __future__ import annotations

import contextvars
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional, Protocol

import jwt
from fastapi import HTTPException, Request


# Name of the httpOnly session cookie.
SESSION_COOKIE_NAME = "flowie_session"


# Equivalent to Go's context.WithValue(..., userIDKey, userID).
#
# The value exists only during the authenticated request.
_user_id_ctx: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar(
    "user_id"
)


class SessionRegistry(Protocol):
    """Subset of the session store needed by the auth layer.

    The real implementation uses asyncpg, therefore these methods
    are asynchronous.
    """

    async def is_revoked(self, token_hash: str) -> bool: ...

    async def touch(self, token_hash: str) -> None: ...


def hash_token(token: str) -> str:
    """Return the digest stored for a session token.

    The raw token is never persisted.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_user_id() -> tuple[Optional[uuid.UUID], bool]:
    """Return the authenticated user id for the current request.

    Python equivalent of Go:

        auth.UserID(r.Context())

    Returns:
        (user_id, True) when authenticated.
        (None, False) when no authenticated user is present.
    """
    try:
        return _user_id_ctx.get(), True
    except LookupError:
        return None, False


@dataclass
class Claims:
    """JWT payload for an authenticated session."""

    subject: str = ""
    email: str = ""
    name: str = ""

    # Marks a half-authenticated session:
    # the user has authenticated but still owes a second factor.
    mfa_pending: bool = False

    jti: str = ""
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "Claims":
        return cls(
            subject=payload.get("sub", ""),
            email=payload.get("email", ""),
            name=payload.get("name", ""),
            mfa_pending=payload.get("mfaPending", False),
            jti=payload.get("jti", ""),
            issued_at=_from_ts(payload.get("iat")),
            expires_at=_from_ts(payload.get("exp")),
        )


def _from_ts(ts) -> Optional[datetime]:
    """Convert JWT timestamps to timezone-aware datetime objects."""
    if ts is None:
        return None

    return datetime.fromtimestamp(ts, tz=timezone.utc)


class SessionManager:
    """Issues and verifies signed session tokens and manages cookies."""

    def __init__(
        self,
        secret: str,
        ttl: timedelta,
        secure: bool,
    ) -> None:
        self._secret = secret
        self._ttl = ttl
        self._secure = secure

        self.registry: Optional[SessionRegistry] = None

    def use_registry(self, registry: SessionRegistry) -> None:
        """Enable DB-backed session revocation checks."""
        self.registry = registry

    @property
    def ttl(self) -> timedelta:
        """Configured session lifetime."""
        return self._ttl

    async def require_auth(
        self,
        request: Request,
    ) -> AsyncIterator[uuid.UUID]:
        """FastAPI dependency that requires an authenticated session.

        Equivalent to Go's:

            SessionManager.RequireAuth(...)

        The authenticated user id is stored in a ContextVar for the
        duration of the endpoint call so handlers can later call:

            get_user_id()
        """

        token = token_from_request(request)

        if not token:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthenticated",
                    "message": "missing session",
                },
            )

        try:
            claims = self.verify(token)
        except Exception:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthenticated",
                    "message": "invalid session",
                },
            )

        try:
            user_id = uuid.UUID(str(claims.subject))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthenticated",
                    "message": "invalid subject",
                },
            )

        # A half-authenticated session may only complete the MFA challenge.
        if claims.mfa_pending:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "mfa_required",
                    "message": "two-factor verification required",
                },
            )

        # Honour remote session revocation when a registry is configured.
        if self.registry is not None:
            token_hash = hash_token(token)

            try:
                revoked = await self.registry.is_revoked(token_hash)
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "session_check_failed",
                        "message": str(exc),
                    },
                )

            if revoked:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": "session_revoked",
                        "message": "session has been revoked",
                    },
                )

            # SessionStore.touch() already handles its own update logic.
            await self.registry.touch(token_hash)

        # Make the authenticated id available to handlers.
        context_token = _user_id_ctx.set(user_id)

        try:
            # Using yield is important:
            # FastAPI keeps this dependency alive until the endpoint finishes.
            yield user_id
        finally:
            _user_id_ctx.reset(context_token)

    def issue(
        self,
        user_id: uuid.UUID,
        email: str,
        name: str,
    ) -> str:
        """Create a fully-authenticated signed token."""
        return self._issue(
            user_id,
            email,
            name,
            mfa_pending=False,
        )

    def issue_pending(
        self,
        user_id: uuid.UUID,
        email: str,
        name: str,
    ) -> str:
        """Create a short-lived token used while completing MFA."""
        return self._issue(
            user_id,
            email,
            name,
            mfa_pending=True,
        )

    def _issue(
        self,
        user_id: uuid.UUID,
        email: str,
        name: str,
        mfa_pending: bool,
    ) -> str:
        now = datetime.now(tz=timezone.utc)

        ttl = self._ttl

        if mfa_pending:
            # MFA challenge tokens should be short-lived.
            ttl = timedelta(minutes=10)

        payload = {
            "sub": str(user_id),
            "email": email,
            "name": name,
            "mfaPending": mfa_pending,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + ttl,
        }

        return jwt.encode(
            payload,
            self._secret,
            algorithm="HS256",
        )

    def verify(self, token: str) -> Claims:
        """Parse and validate a signed session token."""
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
            )
        except jwt.PyJWTError as exc:
            raise ValueError(str(exc)) from exc

        return Claims.from_payload(payload)

    def set_cookie(
        self,
        response,
        token: str,
    ) -> None:
        """Write the session cookie to a response."""

        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            path="/",
            httponly=True,
            secure=self._secure,
            samesite="Lax",
            expires=datetime.now(tz=timezone.utc) + self._ttl,
        )

    def clear_cookie(self, response) -> None:
        """Remove the session cookie."""

        response.set_cookie(
            SESSION_COOKIE_NAME,
            "",
            path="/",
            httponly=True,
            secure=self._secure,
            samesite="Lax",
            max_age=0,
        )


def token_from_request(request: Request) -> str:
    """Extract a session token from cookie or Authorization header."""

    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)

    if cookie_value:
        return cookie_value

    authorization = request.headers.get("Authorization", "")

    prefix = "Bearer "

    if authorization.startswith(prefix):
        return authorization[len(prefix):]

    return ""