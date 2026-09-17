"""
Integration store — Python port of the Go `store.IntegrationStore` type.

Persists Slack/Teams incoming-webhook integrations, using asyncpg for
database access.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union

import asyncpg

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")

# Columns shared by every SELECT / RETURNING clause below.
INTEGRATION_COLUMNS = (
    "id, project_id, provider, webhook_url, events, active, last_status, last_error, created_at"
)


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.Integration` type)
# ---------------------------------------------------------------------------

@dataclass
class Integration:
    id: uuid.UUID
    project_id: uuid.UUID
    provider: str
    webhook_url: str
    active: bool
    last_status: Optional[int]
    last_error: Optional[str]
    created_at: datetime
    events: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Row scanning helper (equivalent of the Go `scanIntegration` function)
# ---------------------------------------------------------------------------

def _scan_integration(row: Union[asyncpg.Record, tuple]) -> Integration:
    """Build an Integration from a row returned in INTEGRATION_COLUMNS order."""
    (
        integration_id,
        project_id,
        provider,
        webhook_url,
        events_raw,
        active,
        last_status,
        last_error,
        created_at,
    ) = row

    events: list[str] = []
    if events_raw:
        events = events_raw if isinstance(events_raw, list) else json.loads(events_raw)

    return Integration(
        id=integration_id,
        project_id=project_id,
        provider=provider,
        webhook_url=webhook_url,
        active=active,
        last_status=last_status,
        last_error=last_error,
        created_at=created_at,
        events=events,
    )


# ---------------------------------------------------------------------------
# IntegrationStore
# ---------------------------------------------------------------------------

class IntegrationStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_by_project(self, project_id: uuid.UUID) -> list[Integration]:
        """Return a project's integrations."""
        rows = await self.pool.fetch(
            f"SELECT {INTEGRATION_COLUMNS} FROM integrations WHERE project_id=$1 ORDER BY created_at",
            project_id,
        )
        return [_scan_integration(row) for row in rows]

    async def active_for_event(self, project_id: uuid.UUID, event_type: str) -> list[Integration]:
        """Return integrations that want a given event type."""
        rows = await self.pool.fetch(
            f"""
            SELECT {INTEGRATION_COLUMNS} FROM integrations
            WHERE project_id=$1 AND active
              AND (events = '[]'::jsonb OR events @> to_jsonb($2::text))
            """,
            project_id,
            event_type,
        )
        return [_scan_integration(row) for row in rows]

    async def create(
        self,
        project_id: uuid.UUID,
        provider: str,
        url: str,
        events: Optional[list[str]],
    ) -> Integration:
        """Register an integration."""
        events = events or []
        raw = json.dumps(events)

        row = await self.pool.fetchrow(
            f"""
            INSERT INTO integrations (project_id, provider, webhook_url, events)
            VALUES ($1,$2,$3,$4)
            RETURNING {INTEGRATION_COLUMNS}
            """,
            project_id,
            provider,
            url,
            raw,
        )
        return _scan_integration(row)

    async def delete(self, project_id: uuid.UUID, integration_id: uuid.UUID) -> None:
        """Remove an integration scoped to its project."""
        result = await self.pool.execute(
            "DELETE FROM integrations WHERE id=$1 AND project_id=$2",
            integration_id,
            project_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def record_delivery(self, integration_id: uuid.UUID, status: int, err_msg: str) -> None:
        """
        Store the outcome of the last post.

        Failures are swallowed: recording a delivery outcome must never
        break the caller, and the error is not actionable at the call site.
        """
        err_val = err_msg if err_msg else None
        try:
            await self.pool.execute(
                "UPDATE integrations SET last_status=$2, last_error=$3 WHERE id=$1",
                integration_id,
                status,
                err_val,
            )
        except Exception:
            # Intentionally swallowed — see docstring.
            logger.exception("integration: failed to record delivery id=%s", integration_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0