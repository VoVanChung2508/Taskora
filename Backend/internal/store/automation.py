"""
Automation-rule persistence.

Supports both:
- v1 rules: status_changed -> assign
- v2 rules: Trigger -> Conditions -> Actions DSL
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go ErrNotFound sentinel."""


ErrNotFound = NotFoundError("not found")


AUTOMATION_COLUMNS = (
    "id, project_id, name, trigger_type, trigger_status, action_type, "
    "action_assignee_id, conditions, actions, active, created_at"
)


@dataclass
class AutomationCondition:
    field: str = ""
    op: str = ""
    value: str = ""


@dataclass
class AutomationAction:
    type: str = ""
    user_id: str = ""
    value: str = ""
    message: str = ""


@dataclass
class AutomationRule:
    id: Optional[uuid.UUID]
    project_id: uuid.UUID
    name: str
    trigger_type: str
    trigger_status: str
    action_type: str
    action_assignee_id: Optional[uuid.UUID]
    active: bool
    created_at: Optional[datetime] = None
    conditions: list[AutomationCondition] = field(default_factory=list)
    actions: list[AutomationAction] = field(default_factory=list)


def _decode_json_array(raw: Any) -> list[Any]:
    """Decode a JSON/JSONB array returned by asyncpg."""
    if not raw:
        return []

    if isinstance(raw, list):
        return raw

    if isinstance(raw, str):
        value = json.loads(raw)
        return value if isinstance(value, list) else []

    return []


def _condition_from_json(value: Any) -> AutomationCondition:
    if isinstance(value, AutomationCondition):
        return value

    if not isinstance(value, dict):
        return AutomationCondition()

    # Accept "operator" too so rows created by the old Python migration
    # remain readable, but write the canonical Go/API key "op".
    return AutomationCondition(
        field=str(value.get("field") or ""),
        op=str(value.get("op") or value.get("operator") or ""),
        value=str(value.get("value") or ""),
    )


def _action_from_json(value: Any) -> AutomationAction:
    if isinstance(value, AutomationAction):
        return value

    if not isinstance(value, dict):
        return AutomationAction()

    # Go/API JSON uses userId; the older Python port wrote user_id.
    # Read both so existing rows continue to work.
    return AutomationAction(
        type=str(value.get("type") or ""),
        user_id=str(value.get("userId") or value.get("user_id") or ""),
        value=str(value.get("value") or ""),
        message=str(value.get("message") or ""),
    )


def _condition_to_json(value: Any) -> dict[str, Any]:
    condition = _condition_from_json(value)
    return {
        "field": condition.field,
        "op": condition.op,
        "value": condition.value,
    }


def _action_to_json(value: Any) -> dict[str, Any]:
    action = _action_from_json(value)
    return {
        "type": action.type,
        "userId": action.user_id,
        "value": action.value,
        "message": action.message,
    }


def _scan_rule(row: Union[asyncpg.Record, tuple]) -> AutomationRule:
    """Build an AutomationRule from AUTOMATION_COLUMNS order."""
    (
        rule_id,
        project_id,
        name,
        trigger_type,
        trigger_status,
        action_type,
        action_assignee_id,
        conditions_raw,
        actions_raw,
        active,
        created_at,
    ) = row

    return AutomationRule(
        id=rule_id,
        project_id=project_id,
        name=name,
        trigger_type=trigger_type,
        trigger_status=trigger_status,
        action_type=action_type,
        action_assignee_id=action_assignee_id,
        active=active,
        created_at=created_at,
        conditions=[
            _condition_from_json(item)
            for item in _decode_json_array(conditions_raw)
        ],
        actions=[
            _action_from_json(item)
            for item in _decode_json_array(actions_raw)
        ],
    )


class AutomationStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        project_id: uuid.UUID,
        name: str,
        trigger_status: str,
        assignee: Optional[uuid.UUID],
    ) -> AutomationRule:
        """Create the legacy v1 status -> assign rule."""
        actions = [
            {
                "type": "assign",
                "userId": str(assignee) if assignee is not None else "",
                "value": "",
                "message": "",
            }
        ]

        row = await self.pool.fetchrow(
            f"""
            INSERT INTO automation_rules (
                project_id,
                name,
                trigger_type,
                trigger_status,
                action_type,
                action_assignee_id,
                actions
            )
            VALUES ($1,$2,'status_changed',$3,'assign',$4,$5)
            RETURNING {AUTOMATION_COLUMNS}
            """,
            project_id,
            name,
            trigger_status,
            assignee,
            json.dumps(actions),
        )

        return _scan_rule(row)

    async def create_v2(
        self,
        project_id: uuid.UUID,
        name: str,
        trigger_type: str,
        trigger_status: str,
        conds: Optional[list[AutomationCondition]],
        acts: Optional[list[AutomationAction]],
    ) -> AutomationRule:
        """Create a Trigger -> Conditions -> Actions rule."""
        conditions_json = [
            _condition_to_json(item)
            for item in (conds or [])
        ]
        actions_json = [
            _action_to_json(item)
            for item in (acts or [])
        ]

        row = await self.pool.fetchrow(
            f"""
            INSERT INTO automation_rules (
                project_id,
                name,
                trigger_type,
                trigger_status,
                action_type,
                conditions,
                actions
            )
            VALUES ($1,$2,$3,$4,'multi',$5,$6)
            RETURNING {AUTOMATION_COLUMNS}
            """,
            project_id,
            name,
            trigger_type,
            trigger_status,
            json.dumps(conditions_json),
            json.dumps(actions_json),
        )

        return _scan_rule(row)

    async def list_by_project(
        self,
        project_id: uuid.UUID,
    ) -> list[AutomationRule]:
        rows = await self.pool.fetch(
            f"""
            SELECT {AUTOMATION_COLUMNS}
            FROM automation_rules
            WHERE project_id=$1
            ORDER BY created_at
            """,
            project_id,
        )
        return [_scan_rule(row) for row in rows]

    async def active_for_trigger(
        self,
        project_id: uuid.UUID,
        status: str,
    ) -> list[AutomationRule]:
        """
        Return active status_changed rules matching the task's new status.

        This mirrors the current Go automation engine, which invokes the
        engine with task.Status and performs one non-recursive rule pass.
        """
        rows = await self.pool.fetch(
            f"""
            SELECT {AUTOMATION_COLUMNS}
            FROM automation_rules
            WHERE project_id=$1
              AND active
              AND trigger_type='status_changed'
              AND trigger_status=$2
            """,
            project_id,
            status,
        )
        return [_scan_rule(row) for row in rows]

    async def project_id_for_rule(
        self,
        rule_id: uuid.UUID,
    ) -> uuid.UUID:
        """
        Resolve a rule's project before deleting it.

        The handler uses this to authorize the caller before the destructive
        DELETE operation.
        """
        project_id = await self.pool.fetchval(
            "SELECT project_id FROM automation_rules WHERE id=$1",
            rule_id,
        )

        if project_id is None:
            raise NotFoundError

        return project_id

    async def delete(
        self,
        rule_id: uuid.UUID,
    ) -> uuid.UUID:
        """Delete a rule and return its project id."""
        row = await self.pool.fetchrow(
            """
            DELETE FROM automation_rules
            WHERE id=$1
            RETURNING project_id
            """,
            rule_id,
        )

        if row is None:
            raise NotFoundError

        return row["project_id"]
