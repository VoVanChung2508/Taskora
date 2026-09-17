import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


@dataclass
class CustomFieldDef:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    field_type: str
    options: Optional[Any] = None


@dataclass
class CustomFieldValue:
    field_id: uuid.UUID
    name: str
    field_type: str
    options: Optional[Any] = None
    value: Optional[Any] = None


def _parse_json(raw: Any) -> Optional[Any]:
    """Some drivers/columns return jsonb as a str, others as already-decoded
    data (if a type codec is registered). Normalize to Python data or None."""
    if raw is None:
        return None
    if isinstance(raw, (str, bytes, bytearray)):
        if not raw:
            return None
        return json.loads(raw)
    return raw


class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_custom_field_defs(self, project_id: uuid.UUID) -> list[CustomFieldDef]:
        """Returns a project's custom field definitions."""
        rows = await self.pool.fetch(
            """
            SELECT id, project_id, name, field_type, options
            FROM custom_field_defs
            WHERE project_id = $1
            ORDER BY created_at
            """,
            project_id,
        )
        out = []
        for row in rows:
            out.append(
                CustomFieldDef(
                    id=row["id"],
                    project_id=row["project_id"],
                    name=row["name"],
                    field_type=row["field_type"],
                    options=_parse_json(row["options"]),
                )
            )
        return out

    async def create_custom_field_def(
        self,
        project_id: uuid.UUID,
        name: str,
        field_type: str,
        options: Optional[Any] = None,
    ) -> CustomFieldDef:
        """Adds a field definition to a project."""
        opts_arg = json.dumps(options) if options is not None else None
        row = await self.pool.fetchrow(
            """
            INSERT INTO custom_field_defs (project_id, name, field_type, options)
            VALUES ($1, $2, $3, $4)
            RETURNING id, project_id, name, field_type, options
            """,
            project_id,
            name,
            field_type,
            opts_arg,
        )
        return CustomFieldDef(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            field_type=row["field_type"],
            options=_parse_json(row["options"]),
        )

    async def delete_custom_field_def(self, project_id: uuid.UUID, field_id: uuid.UUID) -> None:
        """Removes a field definition (scoped to its project)."""
        result = await self.pool.execute(
            "DELETE FROM custom_field_defs WHERE id = $1 AND project_id = $2",
            field_id,
            project_id,
        )
        if result.split()[-1] == "0":
            raise NotFoundError

    async def list_custom_field_values(self, task_id: uuid.UUID) -> list[CustomFieldValue]:
        """Returns every field definition in the task's project paired with
        the task's current value (None when unset)."""
        rows = await self.pool.fetch(
            """
            SELECT d.id, d.name, d.field_type, d.options, v.value
            FROM custom_field_defs d
            JOIN tasks t ON t.id = $1 AND t.project_id = d.project_id
            LEFT JOIN custom_field_values v ON v.custom_field_id = d.id AND v.task_id = $1
            ORDER BY d.created_at
            """,
            task_id,
        )
        out = []
        for row in rows:
            out.append(
                CustomFieldValue(
                    field_id=row["id"],
                    name=row["name"],
                    field_type=row["field_type"],
                    options=_parse_json(row["options"]),
                    value=_parse_json(row["value"]),
                )
            )
        return out

    async def set_custom_field_value(
        self, task_id: uuid.UUID, field_id: uuid.UUID, value: Any
    ) -> None:
        """Upserts a task's value for a field. Only writes when the field
        belongs to the task's project; raises NotFoundError otherwise."""
        raw = json.dumps(value)
        result = await self.pool.execute(
            """
            INSERT INTO custom_field_values (task_id, custom_field_id, value)
            SELECT $1, d.id, $3
            FROM custom_field_defs d
            JOIN tasks t ON t.id = $1 AND t.project_id = d.project_id
            WHERE d.id = $2
            ON CONFLICT (task_id, custom_field_id) DO UPDATE SET value = EXCLUDED.value
            """,
            task_id,
            field_id,
            raw,
        )
        if result.split()[-1] == "0":
            raise NotFoundError

    async def clear_custom_field_value(self, task_id: uuid.UUID, field_id: uuid.UUID) -> None:
        """Removes a task's value for a field."""
        await self.pool.execute(
            "DELETE FROM custom_field_values WHERE task_id = $1 AND custom_field_id = $2",
            task_id,
            field_id,
        )