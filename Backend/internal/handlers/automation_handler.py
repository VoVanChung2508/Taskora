from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.automation import (
    AutomationAction,
    AutomationCondition,
    NotFoundError as AutomationNotFoundError,
)
from .rbac import can_manage_workspace

if TYPE_CHECKING:
    from .handlers import Handlers


TRIGGER_STATUS_CHANGED = "status_changed"

VALID_ACTION_TYPES = {
    "assign",
    "set_status",
    "set_priority",
    "notify",
}
VALID_CONDITION_OPS = {
    "eq",
    "neq",
    "is_empty",
    "not_empty",
    "gt",
    "lt",
}
VALID_CONDITION_FIELDS = {
    "priority",
    "status",
    "assignee",
    "story_points",
    "moscow",
}


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
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


async def list_automations(
    self: "Handlers",
    project_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    try:
        rules = await self.store.automations.list_by_project(
            project.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"rules": rules or []}


async def create_automation(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only owner/admin can manage automations",
            },
        )

    data = await _read_json_object(
        request,
        {"name", "triggerStatus", "assigneeId"},
    )

    name = data.get("name", "")
    trigger_status = data.get("triggerStatus", "")
    raw_assignee_id = data.get("assigneeId")

    if not isinstance(name, str) or not isinstance(trigger_status, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name and triggerStatus must be strings",
            },
        )

    trigger_status = trigger_status.strip()

    if not trigger_status or raw_assignee_id in (None, ""):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "triggerStatus and assigneeId required",
            },
        )

    if not isinstance(raw_assignee_id, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "assigneeId must be a UUID",
            },
        )

    try:
        assignee_id = uuid.UUID(raw_assignee_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "assigneeId must be a UUID",
            },
        ) from exc

    try:
        rule = await self.store.automations.create(
            project.id,
            name.strip(),
            trigger_status,
            assignee_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(rule),
    )


def _parse_conditions(raw: Any) -> list[AutomationCondition]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "conditions must be an array",
            },
        )

    out: list[AutomationCondition] = []

    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": "condition must be an object",
                },
            )

        unknown = set(item) - {"field", "op", "value"}
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": f"unknown condition field(s): {sorted(unknown)}",
                },
            )

        field = item.get("field", "")
        op = item.get("op", "")
        value = item.get("value", "")

        if not all(isinstance(v, str) for v in (field, op, value)):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": "condition field/op/value must be strings",
                },
            )

        if field not in VALID_CONDITION_FIELDS or op not in VALID_CONDITION_OPS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation",
                    "message": "invalid condition field/op",
                },
            )

        out.append(
            AutomationCondition(
                field=field,
                op=op,
                value=value,
            )
        )

    return out


def _parse_actions(raw: Any) -> list[AutomationAction]:
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "actions must be an array",
            },
        )

    out: list[AutomationAction] = []

    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": "action must be an object",
                },
            )

        unknown = set(item) - {"type", "userId", "value", "message"}
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": f"unknown action field(s): {sorted(unknown)}",
                },
            )

        action_type = item.get("type", "")
        user_id = item.get("userId", "")
        value = item.get("value", "")
        message = item.get("message", "")

        if not all(
            isinstance(v, str)
            for v in (action_type, user_id, value, message)
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_body",
                    "message": "action fields must be strings",
                },
            )

        if action_type not in VALID_ACTION_TYPES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation",
                    "message": "unknown action type: " + action_type,
                },
            )

        if action_type in {"assign", "notify"} and not user_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation",
                    "message": action_type + " requires userId",
                },
            )

        if action_type in {"assign", "notify"}:
            try:
                uuid.UUID(user_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "validation",
                        "message": action_type + " requires valid userId",
                    },
                ) from exc

        if action_type in {"set_status", "set_priority"} and not value:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "validation",
                    "message": action_type + " requires value",
                },
            )

        out.append(
            AutomationAction(
                type=action_type,
                user_id=user_id,
                value=value,
                message=message,
            )
        )

    return out


async def create_automation_v2(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    project, role = await self.require_project_access(
        project_id,
        user_id,
    )

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "only owner/admin can manage automations",
            },
        )

    data = await _read_json_object(
        request,
        {
            "name",
            "triggerType",
            "triggerStatus",
            "conditions",
            "actions",
        },
    )

    name = data.get("name", "")
    trigger_type = data.get("triggerType", "")
    trigger_status = data.get("triggerStatus", "")

    if not all(
        isinstance(v, str)
        for v in (name, trigger_type, trigger_status)
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "name/triggerType/triggerStatus must be strings",
            },
        )

    name = name.strip()
    trigger_status = trigger_status.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "name is required"},
        )

    if not trigger_type:
        trigger_type = TRIGGER_STATUS_CHANGED

    if (
        trigger_type == TRIGGER_STATUS_CHANGED
        and not trigger_status
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "triggerStatus is required for status_changed",
            },
        )

    actions = _parse_actions(data.get("actions"))
    if not actions:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "at least one action is required",
            },
        )

    conditions = _parse_conditions(
        data.get("conditions", [])
    )

    try:
        rule = await self.store.automations.create_v2(
            project.id,
            name,
            trigger_type,
            trigger_status,
            conditions,
            actions,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(rule),
    )


async def delete_automation(
    self: "Handlers",
    rule_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()

    # Authorization is deliberately checked BEFORE deletion.  The Go source
    # deletes first and checks the user's role afterwards, which lets an
    # unauthorized caller destroy a rule even though the response is 403.
    try:
        project_id = await self.store.automations.project_id_for_rule(
            rule_id
        )
    except AutomationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "rule not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "lookup_failed", "message": str(exc)},
        ) from exc

    try:
        project = await self.store.projects.get_by_id(project_id)
        role = await self.store.workspaces.role_for_user(
            project.workspace_id,
            user_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "insufficient role"},
        ) from exc

    if not can_manage_workspace(role):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "insufficient role"},
        )

    try:
        await self.store.automations.delete(rule_id)
    except AutomationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "rule not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}
