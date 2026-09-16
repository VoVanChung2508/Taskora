"""Auth middleware: rejects unauthenticated requests and injects the
authenticated user id into the request context.

Python port of the Go `auth` middleware (`RequireAuth` / `UserID`).
Framework-agnostic: wraps any handler with the signature
``handler(request, *args, **kwargs) -> response``. Go's per-request
`context.Context` value is modeled with `contextvars.ContextVar`, which is
the closest Python equivalent (safe across async tasks and threads).

Assumes, like the original Go file, that these already exist elsewhere in
the package:
  - `token_from_request(request)`  -> str
  - `hash_token(token)`            -> str
  - `SessionManager.verify(token)` -> claims object with
        `.subject`, `.mfa_pending`
  - `SessionManager.registry`      -> optional object with
        `.is_revoked(token_hash)` and `.touch(token_hash)`
  - `httpx.error(status, code, message)` -> a response object
"""

from __future__ import annotations

import contextvars
import uuid
from typing import Callable, Optional, Tuple

from . import httpx  # local `httpx.error(...)`-style error helper


# Equivalent of Go's `ctxKey`/`userIDKey` + `context.WithValue`.
_user_id_ctx: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("user_id")


class SessionManager:
    """Existing session manager; shown partially here for context."""

    registry: Optional[object] = None

    def verify(self, token: str):
        ...

    def require_auth(self, next_handler: Callable) -> Callable:
        """Middleware that rejects unauthenticated requests and injects the
        authenticated user id into the request context.
        """

        def wrapped(request, *args, **kwargs):
            tok = token_from_request(request)
            if not tok:
                return httpx.error(401, "unauthenticated", "missing session")

            try:
                claims = self.verify(tok)
            except Exception:
                return httpx.error(401, "unauthenticated", "invalid session")

            try:
                user_id = uuid.UUID(str(claims.subject))
            except (ValueError, AttributeError, TypeError):
                return httpx.error(401, "unauthenticated", "invalid subject")

            # A half-authenticated session may only finish the MFA challenge.
            if getattr(claims, "mfa_pending", False):
                return httpx.error(
                    401, "mfa_required", "two-factor verification required"
                )

            # Honour remote revocation when a session registry is configured.
            if self.registry is not None:
                token_hash = hash_token(tok)
                try:
                    revoked = self.registry.is_revoked(token_hash)
                except Exception as exc:
                    return httpx.error(500, "session_check_failed", str(exc))
                if revoked:
                    return httpx.error(
                        401, "session_revoked", "session has been revoked"
                    )
                self.registry.touch(token_hash)

            reset_token = _user_id_ctx.set(user_id)
            try:
                return next_handler(request, *args, **kwargs)
            finally:
                _user_id_ctx.reset(reset_token)

        return wrapped


def get_user_id() -> Tuple[Optional[uuid.UUID], bool]:
    """Extract the authenticated user id from the current context.

    Equivalent of Go's `UserID(ctx)`.
    """
    try:
        return _user_id_ctx.get(), True
    except LookupError:
        return None, False


def token_from_request(request) -> str:
    """Extract the bearer/session token from the incoming request.

    Adapt this to your framework, e.g. for Flask/FastAPI-style requests:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[len("Bearer "):]
        return request.cookies.get("session", "")
    """
    raise NotImplementedError


def hash_token(token: str) -> str:
    """Hash a session token before looking it up in the registry."""
    raise NotImplementedError