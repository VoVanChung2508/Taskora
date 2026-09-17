"""
Automation store — Python port of the Go `store.AutomationStore` type.

Handles persistence for automation rules, using asyncpg for database access.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Union

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")


# Columns shared by every SELECT / RETURNING clause below.
AUTOMATION_COLUMNS = (
    "id, project_id, name, trigger_type, trigger_status, action_type, "
    "action_assignee_id, conditions, actions, active, created_at"
)


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

@dataclass
class AutomationCondition:
    # Shape left open-ended, mirroring `domain.AutomationCondition`'s
    # unspecified fields in the original snippet — adjust as needed.
    field: str = ""
    operator: str = ""
    value: Any = None


@dataclass
class AutomationAction:
    type: str = ""
    user_id: str = ""


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


# ---------------------------------------------------------------------------
# Row scanning helper (equivalent of the Go `scanRule` function)
# ---------------------------------------------------------------------------

def _scan_rule(row: Union[asyncpg.Record, tuple]) -> AutomationRule:
    """Build an AutomationRule from a row returned in AUTOMATION_COLUMNS order."""
    (
        rule_id,
        project_id,
        name,
        trigger_type,
        trigger_status,
        action_type,
        action_assignee_id,
        conds_raw,
        acts_raw,
        active,
        created_at,
    ) = row

    a = AutomationRule(
        id=rule_id,
        project_id=project_id,
        name=name,
        trigger_type=trigger_type,
        trigger_status=trigger_status,
        action_type=action_type,
        action_assignee_id=action_assignee_id,
        active=active,
        created_at=created_at,
    )

    if conds_raw:
        raw = conds_raw if isinstance(conds_raw, list) else json.loads(conds_raw)
        a.conditions = [AutomationCondition(**c) if isinstance(c, dict) else c for c in raw]

    if acts_raw:
        raw = acts_raw if isinstance(acts_raw, list) else json.loads(acts_raw)
        a.actions = [
            AutomationAction(type=x.get("type", ""), user_id=x.get("user_id", ""))
            if isinstance(x, dict)
            else x
            for x in raw
        ]

    return a


# ---------------------------------------------------------------------------
# AutomationStore
# ---------------------------------------------------------------------------

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
        """Insert an automation rule (v1 shape: status trigger + assign action)."""
        actions = [{"type": "assign", "user_id": str(assignee)}]
        raw = json.dumps(actions)

        row = await self.pool.fetchrow(
            f"""
            INSERT INTO automation_rules (project_id, name, trigger_type, trigger_status, action_type, action_assignee_id, actions)
            VALUES ($1,$2,'status_changed',$3,'assign',$4,$5)
            RETURNING {AUTOMATION_COLUMNS}
            """,
            project_id,
            name,
            trigger_status,
            assignee,
            raw,
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
        """Insert a rule with explicit conditions and actions."""
        conds = conds or []
        acts = acts or []
        raw_c = json.dumps([_dataclass_to_dict(c) for c in conds])
        raw_a = json.dumps([_dataclass_to_dict(a) for a in acts])

        row = await self.pool.fetchrow(
            f"""
            INSERT INTO automation_rules (project_id, name, trigger_type, trigger_status, action_type, conditions, actions)
            VALUES ($1,$2,$3,$4,'multi',$5,$6)
            RETURNING {AUTOMATION_COLUMNS}
            """,
            project_id,
            name,
            trigger_type,
            trigger_status,
            raw_c,
            raw_a,
        )
        return _scan_rule(row)

    async def list_by_project(self, project_id: uuid.UUID) -> list[AutomationRule]:
        """Return all rules in a project."""
        rows = await self.pool.fetch(
            f"SELECT {AUTOMATION_COLUMNS} FROM automation_rules WHERE project_id=$1 ORDER BY created_at",
            project_id,
        )
        return [_scan_rule(row) for row in rows]

    async def active_for_trigger(self, project_id: uuid.UUID, status: str) -> list[AutomationRule]:
        """
        Return active rules matching a project + trigger.

        For status triggers the status must match; other triggers ignore it.
        """
        rows = await self.pool.fetch(
            f"""
            SELECT {AUTOMATION_COLUMNS} FROM automation_rules
             WHERE project_id=$1 AND active
               AND trigger_type = 'status_changed' AND trigger_status=$2
            """,
            project_id,
            status,
        )
        return [_scan_rule(row) for row in rows]

    async def delete(self, rule_id: uuid.UUID) -> uuid.UUID:
        """Remove a rule, returning its project id for authorization."""
        row = await self.pool.fetchrow(
            "DELETE FROM automation_rules WHERE id=$1 RETURNING project_id",
            rule_id,
        )
        if row is None:
            raise ErrNotFound
        return row["project_id"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dataclass_to_dict(obj: Any) -> Any:
    """Serialize an AutomationCondition/AutomationAction (or a plain dict) to JSON-able data."""
    if isinstance(obj, (AutomationCondition, AutomationAction)):
        return obj.__dict__
    return obj