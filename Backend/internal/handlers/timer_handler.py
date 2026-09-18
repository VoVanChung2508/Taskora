from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.timers import (
    NotFoundError as TimerNotFoundError,
    TimerRunningError,
)

if TYPE_CHECKING:
    from .handlers import Handlers


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


async def _read_optional_timer_body(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
    except Exception:
        return {}

    if not raw:
        return {}

    try:
        data = await request.json()
    except Exception:
        # Go ignores Decode errors here because the body is optional.
        return {}

    if not isinstance(data, dict):
        return {}

    return data


async def start_timer(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, role = await self.require_task_access(task_id, user_id)

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot log time",
            },
        )

    data = await _read_optional_timer_body(request)
    note = data.get("note", "")
    if not isinstance(note, str):
        note = ""

    try:
        timer = await self.store.worklogs.start_timer(
            user_id,
            task.id,
            note.strip(),
        )
    except TimerRunningError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "timer_running",
                "message": "một bộ đếm khác đang chạy — hãy dừng nó trước",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "start_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(timer),
    )


async def get_active_timer(
    self: "Handlers",
) -> dict:
    user_id = _current_user_id()

    try:
        timer = await self.store.worklogs.active_timer(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "lookup_failed", "message": str(exc)},
        ) from exc

    return {"timer": timer}


async def stop_timer(
    self: "Handlers",
    request: Request,
):
    user_id = _current_user_id()

    data = await _read_optional_timer_body(request)
    note = data.get("note", "")
    if not isinstance(note, str):
        note = ""

    try:
        worklog = await self.store.worklogs.stop_timer(
            user_id,
            note.strip(),
        )
    except TimerNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_timer",
                "message": "không có bộ đếm nào đang chạy",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "stop_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            worklog.task_id,
            user_id,
            "logged_time",
            {
                "minutes": worklog.minutes,
                "source": "timer",
            },
        )
    except Exception:
        pass

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(worklog),
    )


async def cancel_timer(
    self: "Handlers",
) -> dict:
    user_id = _current_user_id()

    try:
        await self.store.worklogs.cancel_timer(user_id)
    except TimerNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_timer",
                "message": "không có bộ đếm nào đang chạy",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "cancel_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
