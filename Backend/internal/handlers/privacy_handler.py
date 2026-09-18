from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.audit import (
    AUDIT_DATA_EXPORTED,
    AUDIT_ACCOUNT_ERASED,
)
from .auth_handler import _clear_session_cookie

if TYPE_CHECKING:
    from .handlers import Handlers


def _current_user_id():
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )
    return user_id


async def _read_confirm(request: Request) -> str:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": str(exc)},
        ) from exc

    if not isinstance(data, dict) or set(data) - {"confirm"}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "expected JSON object with confirm",
            },
        )

    confirm = data.get("confirm", "")
    if not isinstance(confirm, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "confirm must be a string",
            },
        )
    return confirm


async def export_my_data(
    self: "Handlers",
    request: Request,
):
    user_id = _current_user_id()

    try:
        bundle = await self.store.users.export_data(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "export_failed", "message": str(exc)},
        ) from exc

    await self.audit(
        request,
        AUDIT_DATA_EXPORTED,
        "self",
        None,
        None,
    )

    filename = f"flowie-export-{date.today().isoformat()}.json"
    return JSONResponse(
        content=jsonable_encoder(bundle),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


async def delete_my_data(
    self: "Handlers",
    request: Request,
):
    user_id = _current_user_id()

    try:
        user = await self.store.users.get_by_id(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "user not found"},
        ) from exc

    confirm = (await _read_confirm(request)).strip()
    if confirm.lower() != user.email.lower():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "confirm_mismatch",
                "message": "nhập đúng email của bạn để xác nhận xoá tài khoản",
            },
        )

    await self.audit_for(
        request,
        user_id,
        user.email,
        AUDIT_ACCOUNT_ERASED,
        "self",
        None,
    )

    try:
        await self.store.users.anonymise_account(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    response = JSONResponse({"deleted": True})
    _clear_session_cookie(self, response)
    return response
