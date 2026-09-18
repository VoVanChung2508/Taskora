from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from .auth_handler import (
    _claim_expiry,
    _issue_session,
    _set_session_cookie,
    _verify_session,
    hash_token,
    record_session,
    token_from_request,
)

if TYPE_CHECKING:
    from .handlers import Handlers


ROTATE_THRESHOLD = timedelta(hours=6)


async def refresh_session(
    self: "Handlers",
    request: Request,
):
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )

    old_token = token_from_request(request)
    if not old_token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthenticated",
                "message": "missing session",
            },
        )

    try:
        claims = await _verify_session(self, old_token)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthenticated",
                "message": "invalid session",
            },
        ) from exc

    expiry = _claim_expiry(claims)
    force = request.query_params.get("force") == "1"
    now = datetime.now(timezone.utc)

    if (
        not force
        and expiry is not None
        and expiry - now > ROTATE_THRESHOLD
    ):
        return {
            "rotated": False,
            "expiresAt": expiry,
        }

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "user not found"},
        ) from exc

    try:
        new_token = await _issue_session(self, user)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "session_failed", "message": str(exc)},
        ) from exc

    try:
        new_claims = await _verify_session(self, new_token)
        new_expiry = _claim_expiry(new_claims)
    except Exception:
        new_expiry = None

    if new_expiry is None:
        new_expiry = now + timedelta(days=7)

    try:
        await self.store.sessions.rotate(
            user_id,
            hash_token(old_token),
            hash_token(new_token),
            new_expiry,
        )
    except Exception:
        await record_session(
            self,
            request,
            user.id,
            new_token,
        )

    response = JSONResponse(
        content={
            "rotated": True,
            "expiresAt": new_expiry.isoformat(),
        }
    )
    _set_session_cookie(self, response, new_token)
    return response

