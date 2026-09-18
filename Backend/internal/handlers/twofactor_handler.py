from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.twofactor import hash_recovery_code
from ..store.audit import (
    AUDIT_MFA_ENABLED,
    AUDIT_MFA_DISABLED,
    AUDIT_MFA_VERIFIED,
)
from .auth_handler import (
    _claim,
    _clear_session_cookie,
    _issue_session,
    _set_session_cookie,
    _verify_session,
    record_session,
    token_from_request,
)

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


async def _read_code(request: Request) -> str:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": str(exc)},
        ) from exc

    if not isinstance(data, dict) or set(data) - {"code"}:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "expected code"},
        )

    code = data.get("code", "")
    if not isinstance(code, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "code must be a string"},
        )
    return code.strip()


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_provisioning_uri(
    secret: str,
    email: str,
    issuer: str = "Flowie",
) -> str:
    label = urllib.parse.quote(f"{issuer}:{email}")
    query = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": "6",
            "period": "30",
        }
    )
    return f"otpauth://totp/{label}?{query}"


def _totp_at(secret: str, unix_time: int) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded.upper())
    counter = unix_time // 30
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify_totp(
    secret: str,
    code: str,
    now: float | None = None,
) -> bool:
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False
    current = int(now if now is not None else time.time())
    for delta in (-30, 0, 30):
        try:
            if hmac.compare_digest(_totp_at(secret, current + delta), code):
                return True
        except Exception:
            return False
    return False


def new_recovery_codes(count: int = 8) -> list[str]:
    return [
        secrets.token_hex(5).upper()
        for _ in range(count)
    ]


async def pending_user_id(
    self: "Handlers",
    request: Request,
) -> tuple[uuid.UUID, str, bool]:
    token = token_from_request(request)
    if not token:
        return uuid.UUID(int=0), "", False

    try:
        claims = await _verify_session(self, token)
    except Exception:
        return uuid.UUID(int=0), "", False

    pending = bool(
        _claim(
            claims,
            "mfa_pending",
            "mfaPending",
            "MFAPending",
            default=False,
        )
    )
    if not pending:
        return uuid.UUID(int=0), "", False

    subject = _claim(
        claims,
        "sub",
        "subject",
        "Subject",
        default="",
    )

    try:
        user_id = uuid.UUID(str(subject))
    except (ValueError, TypeError):
        return uuid.UUID(int=0), "", False

    return user_id, token, True


async def two_factor_status(self: "Handlers") -> dict:
    user_id = _current_user_id()
    try:
        state = await self.store.users.two_factor(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "lookup_failed", "message": str(exc)},
        ) from exc

    return {
        "enabled": state.enabled,
        "recoveryCodesLeft": state.recovery_len,
        "enrolmentStarted": bool(state.secret) and not state.enabled,
    }


async def start_two_factor(self: "Handlers") -> dict:
    user_id = _current_user_id()

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "user not found"},
        ) from exc

    secret = new_totp_secret()

    try:
        await self.store.users.start_totp_enrolment(
            user_id,
            secret,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "save_failed", "message": str(exc)},
        ) from exc

    return {
        "secret": secret,
        "provisioningUri": totp_provisioning_uri(
            secret,
            user.email,
            "Flowie",
        ),
    }


async def enable_two_factor(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()
    code = await _read_code(request)

    try:
        state = await self.store.users.two_factor(user_id)
    except Exception:
        state = None

    if state is None or not state.secret:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_enrolment",
                "message": "hãy bắt đầu thiết lập 2FA trước",
            },
        )

    if not verify_totp(state.secret, code):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_code",
                "message": "mã xác thực không đúng",
            },
        )

    codes = new_recovery_codes(8)
    hashed = [hash_recovery_code(code) for code in codes]

    try:
        await self.store.users.enable_totp(
            user_id,
            hashed,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "enable_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_MFA_ENABLED,
        "totp",
        None,
        None,
    )

    return {
        "enabled": True,
        "recoveryCodes": codes,
    }


async def disable_two_factor(
    self: "Handlers",
    request: Request,
) -> dict:
    user_id = _current_user_id()
    code = await _read_code(request)

    try:
        state = await self.store.users.two_factor(user_id)
    except Exception:
        state = None

    if state is None or not state.enabled:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_enabled",
                "message": "2FA chưa được bật",
            },
        )

    valid = verify_totp(state.secret, code)
    if not valid:
        try:
            valid = await self.store.users.consume_recovery_code(
                user_id,
                code,
            )
        except Exception:
            valid = False

    if not valid:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_code",
                "message": "mã xác thực không đúng",
            },
        )

    try:
        await self.store.users.disable_totp(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "disable_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_MFA_DISABLED,
        "totp",
        None,
        None,
    )

    return {"enabled": False}


async def verify_two_factor(
    self: "Handlers",
    request: Request,
):
    user_id, _pending_token, ok = await pending_user_id(
        self,
        request,
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "no_challenge",
                "message": "không có phiên chờ xác thực 2FA",
            },
        )

    code = await _read_code(request)

    try:
        state = await self.store.users.two_factor(user_id)
    except Exception:
        state = None

    if state is None or not state.enabled:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_enabled",
                "message": "2FA chưa được bật",
            },
        )

    valid = verify_totp(state.secret, code)
    if not valid:
        try:
            valid = await self.store.users.consume_recovery_code(
                user_id,
                code,
            )
        except Exception:
            valid = False

    if not valid:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_code",
                "message": "mã xác thực không đúng",
            },
        )

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "user not found"},
        ) from exc

    try:
        token = await _issue_session(self, user)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "session_failed", "message": str(exc)},
        ) from exc

    response = JSONResponse(content=jsonable_encoder(user))
    _set_session_cookie(self, response, token)
    await record_session(
        self,
        request,
        user.id,
        token,
    )
    await self.audit_for(
        request,
        user.id,
        user.email,
        AUDIT_MFA_VERIFIED,
        "totp",
        None,
    )
    return response
