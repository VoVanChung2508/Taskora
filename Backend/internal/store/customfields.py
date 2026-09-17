"""
Custom fields — Python port of the `store.TaskStore` custom-field-definition
and custom-field-value methods.

Uses asyncpg for database access. `options` and `value` are kept as raw JSON
text (mirroring Go's `json.RawMessage`): they are passed through untouched
rather than being parsed and re-serialized, since this store only stores and
forwards them — callers are responsible for interpreting the JSON.

Note: this assumes asyncpg is used *without* a jsonb type codec registered,
so jsonb columns come back as plain `str` (raw JSON text) rather than
already-decoded Python objects — the direct equivalent of `[]byte` in the Go
version. If your pool registers a jsonb codec, adjust `_as_raw_json`
accordingly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

@dataclass
class CustomFieldDef:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    field_type: str
    options: Optional[str] = None  # raw JSON text, or None when unset


@dataclass
class CustomFieldValue:
    field_id: uuid.UUID
    name: str
    field_type: str
    options: Optional[str] = None  # raw JSON text, or None when unset
    value: Optional[str] = None  # raw JSON text, or None when unset


# ---------------------------------------------------------------------------
# TaskStore custom field methods
# ---------------------------------------------------------------------------

class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_custom_field_defs(self, project_id: uuid.UUID) -> list[CustomFieldDef]:
        """Return a project's custom field definitions."""
        rows = await self.pool.fetch(
            """
            SELECT id, project_id, name, field_type, options
            FROM custom_field_defs
            WHERE project_id = $1
            ORDER BY created_at
            """,
            project_id,
        )

        out: list[CustomFieldDef] = []
        for row in rows:
            out.append(
                CustomFieldDef(
                    id=row["id"],
                    project_id=row["project_id"],
                    name=row["name"],
                    field_type=row["field_type"],
                    options=_as_raw_json(row["options"]),
                )
            )
        return out

    async def create_custom_field_def(
        self,
        project_id: uuid.UUID,
        name: str,
        field_type: str,
        options: Optional[str],
    ) -> CustomFieldDef:
        """Add a field definition to a project."""
        opts_arg = options if options else None

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
            options=_as_raw_json(row["options"]),
        )

    async def delete_custom_field_def(self, project_id: uuid.UUID, field_id: uuid.UUID) -> None:
        """Remove a field definition (scoped to its project)."""
        result = await self.pool.execute(
            "DELETE FROM custom_field_defs WHERE id = $1 AND project_id = $2",
            field_id,
            project_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def list_custom_field_values(self, task_id: uuid.UUID) -> list[CustomFieldValue]:
        """
        Return every field definition in the task's project paired with the
        task's current value (None when unset).
        """
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

        out: list[CustomFieldValue] = []
        for row in rows:
            field_id, name, field_type, opts, val = row[0], row[1], row[2], row[3], row[4]
            out.append(
                CustomFieldValue(
                    field_id=field_id,
                    name=name,
                    field_type=field_type,
                    options=_as_raw_json(opts),
                    value=_as_raw_json(val),
                )
            )
        return out

    async def set_custom_field_value(
        self,
        task_id: uuid.UUID,
        field_id: uuid.UUID,
        value: str,
    ) -> None:
        """
        Upsert a task's value for a field.

        Only writes when the field belongs to the task's project; raises
        ErrNotFound otherwise.
        """
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
            value,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def clear_custom_field_value(self, task_id: uuid.UUID, field_id: uuid.UUID) -> None:
        """Remove a task's value for a field."""
        await self.pool.execute(
            "DELETE FROM custom_field_values WHERE task_id = $1 AND custom_field_id = $2",
            task_id,
            field_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_raw_json(value) -> Optional[str]:
    """
    Pass a jsonb column value through untouched, as raw JSON text.

    Mirrors Go's `if opts != nil { d.Options = json.RawMessage(opts) }`:
    None/empty stays None, anything else is kept as-is (str).
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return value


def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0