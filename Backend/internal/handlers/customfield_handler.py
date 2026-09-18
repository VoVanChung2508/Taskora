from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id

if TYPE_CHECKING:
    from .handlers import Handlers


VALID_FIELD_TYPES = {
    "text",
    "number",
    "dropdown",
    "date",
    "url",
}


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


def _to_raw_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _from_raw_json(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    return value


def _def_payload(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "projectId": item.project_id,
        "name": item.name,
        "fieldType": item.field_type,
        "options": _from_raw_json(item.options),
    }


def _value_payload(item) -> dict[str, Any]:
    return {
        "fieldId": item.field_id,
        "name": item.name,
        "fieldType": item.field_type,
        "options": _from_raw_json(item.options),
        "value": _from_raw_json(item.value),
    }


async def list_custom_fields(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        definitions = await self.store.tasks.list_custom_field_defs(
            project.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {
        "fields": [
            _def_payload(item)
            for item in (definitions or [])
        ]
    }


async def create_custom_field(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot manage custom fields",
            },
        )

    data = await _read_json_object(
        request,
        {"name", "fieldType", "options"},
    )

    name = data.get("name", "")
    field_type = data.get("fieldType", "")

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name must be a string",
            },
        )

    if not isinstance(field_type, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "fieldType must be a string",
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

    if field_type not in VALID_FIELD_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "fieldType must be one of text, number, dropdown, date, url",
            },
        )

    options = (
        _to_raw_json(data["options"])
        if "options" in data
        else None
    )

    try:
        definition = await self.store.tasks.create_custom_field_def(
            project.id,
            name,
            field_type,
            options,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(_def_payload(definition)),
    )


async def delete_custom_field(
    self: "Handlers",
    project_id: uuid.UUID,
    field_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot manage custom fields",
            },
        )

    try:
        await self.store.tasks.delete_custom_field_def(
            project.id,
            field_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def set_task_custom_field(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    task, role = await self.require_task_access(
        task_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot edit tasks",
            },
        )

    data = await _read_json_object(
        request,
        {"fieldId", "value"},
    )

    raw_field_id = data.get("fieldId")

    if not isinstance(raw_field_id, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "fieldId is required",
            },
        )

    try:
        field_id = uuid.UUID(raw_field_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "fieldId must be a UUID",
            },
        ) from exc

    if field_id.int == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "fieldId is required",
            },
        )

    value_present = "value" in data
    value = data.get("value")

    # Matches Go isEmptyJSON: absent, null and JSON "" clear the value.
    if (
        not value_present
        or value is None
        or value == ""
    ):
        try:
            await self.store.tasks.clear_custom_field_value(
                task.id,
                field_id,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "update_failed",
                    "message": str(exc),
                },
            ) from exc
    else:
        raw_value = _to_raw_json(value)

        try:
            await self.store.tasks.set_custom_field_value(
                task.id,
                field_id,
                raw_value,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "update_failed",
                    "message": "field not found in this project",
                },
            ) from exc

    try:
        values = await self.store.tasks.list_custom_field_values(
            task.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {
        "customFields": [
            _value_payload(item)
            for item in (values or [])
        ]
    }
