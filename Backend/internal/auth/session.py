"""Session manager: issues and verifies signed session tokens (JWT) and
manages the session cookie.

Python port of the Go `auth.SessionManager`. Requires:
    pip install PyJWT

This module implements `token_from_request` and `hash_token` for real —
they replace the `NotImplementedError` stubs left in `require_auth.py`.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import jwt

# Name of the httpOnly session cookie.
SESSION_COOKIE_NAME = "flowie_session"


class SessionRegistry(Protocol):
    """Subset of the session store the auth layer needs to enforce remote
    revocation. Optional: when `None` on the manager, sessions are pure JWT.
    """

    def is_revoked(self, token_hash: str) -> bool: ...
    def touch(self, token_hash: str) -> None: ...


def hash_token(token: str) -> str:
    """Return the digest stored for a session token. The raw token is never
    persisted.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Claims:
    """JWT payload for an authenticated session."""

    subject: str = ""
    email: str = ""
    name: str = ""
    # Marks a half-authenticated session: the user proved their identity but
    # still owes a second factor. `require_auth` rejects it.
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
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None


class SessionManager:
    """Issues and verifies signed session tokens (JWT) and manages the
    session cookie.
    """

    def __init__(self, secret: str, ttl: timedelta, secure: bool):
        self._secret = secret
        self._ttl = ttl
        self._secure = secure
        self.registry: Optional[SessionRegistry] = None

    def use_registry(self, registry: SessionRegistry) -> None:
        """Enable DB-backed revocation checks in `require_auth`."""
        self.registry = registry

    @property
    def ttl(self) -> timedelta:
        """Configured session lifetime (used when recording sessions)."""
        return self._ttl

    def issue(self, user_id: uuid.UUID, email: str, name: str) -> str:
        """Create a fully-authenticated signed token for the given user."""
        return self._issue(user_id, email, name, mfa_pending=False)

    def issue_pending(self, user_id: uuid.UUID, email: str, name: str) -> str:
        """Create a token that only allows completing the MFA challenge."""
        return self._issue(user_id, email, name, mfa_pending=True)

    def _issue(
        self, user_id: uuid.UUID, email: str, name: str, mfa_pending: bool
    ) -> str:
        now = datetime.now(tz=timezone.utc)
        ttl = self._ttl
        if mfa_pending:
            # A challenge token is short-lived; it is not a usable session.
            ttl = timedelta(minutes=10)

        payload = {
            "sub": str(user_id),
            "email": email,
            "name": name,
            "mfaPending": mfa_pending,
            # A random ID makes every token unique even if issued within the
            # same second, and gives each session a handle for revocation.
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + ttl,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify(self, token: str) -> Claims:
        """Parse and validate a token, returning its claims."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise ValueError(str(exc)) from exc
        return Claims.from_payload(payload)

    def set_cookie(self, response, token: str) -> None:
        """Write the session cookie on the response.

        `response` must support a Flask/Starlette-style
        `set_cookie(key, value, ...)` method.
        """
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
        """Remove the session cookie (logout)."""
        response.set_cookie(
            SESSION_COOKIE_NAME,
            "",
            path="/",
            httponly=True,
            secure=self._secure,
            samesite="Lax",
            max_age=0,
        )


def token_from_request(request) -> str:
    """Extract the session token from cookie or Bearer header.

    `request` must expose `.cookies` (dict-like) and `.headers` (dict-like),
    as Flask/Starlette/FastAPI request objects do.
    """
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        return cookie_value

    prefix = "Bearer "
    header = request.headers.get("Authorization", "")
    if header.startswith(prefix):
        return header[len(prefix):]

    return ""