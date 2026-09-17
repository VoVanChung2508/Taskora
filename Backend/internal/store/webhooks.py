import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


WEBHOOK_COLUMNS = (
    "id, project_id, url, events, secret, active, last_status, "
    "last_error, last_sent_at, created_at"
)


@dataclass
class Webhook:
    id: uuid.UUID
    project_id: uuid.UUID
    url: str
    secret: str
    active: bool
    last_status: Optional[int]
    last_error: Optional[str]
    last_sent_at: Optional[datetime]
    created_at: datetime
    events: list[str] = field(default_factory=list)
    has_secret: bool = False


def scan_webhook(row: Optional[asyncpg.Record]) -> Webhook:
    if row is None:
        raise NotFoundError

    events_raw = row["events"]
    events: list[str] = []
    if events_raw:
        try:
            events = json.loads(events_raw) if isinstance(events_raw, str) else events_raw
        except (ValueError, TypeError):
            events = []

    secret = row["secret"]
    return Webhook(
        id=row["id"],
        project_id=row["project_id"],
        url=row["url"],
        secret=secret,
        active=row["active"],
        last_status=row["last_status"],
        last_error=row["last_error"],
        last_sent_at=row["last_sent_at"],
        created_at=row["created_at"],
        events=events,
        has_secret=bool(secret),
    )


class WebhookStore:
    """Handles persistence for outgoing webhooks."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_by_project(self, project_id: uuid.UUID) -> list[Webhook]:
        """Returns a project's webhooks."""
        rows = await self.pool.fetch(
            f"SELECT {WEBHOOK_COLUMNS} FROM webhooks WHERE project_id=$1 ORDER BY created_at",
            project_id,
        )
        return [scan_webhook(row) for row in rows]

    async def active_for_event(
        self, project_id: uuid.UUID, event_type: str
    ) -> list[Webhook]:
        """Returns webhooks that should receive a given event type."""
        rows = await self.pool.fetch(
            f"""
            SELECT {WEBHOOK_COLUMNS} FROM webhooks
            WHERE project_id = $1 AND active
              AND (events = '[]'::jsonb OR events @> to_jsonb($2::text))
            """,
            project_id,
            event_type,
        )
        return [scan_webhook(row) for row in rows]

    async def create(
        self,
        project_id: uuid.UUID,
        url: str,
        events: Optional[list[str]],
        secret: str,
    ) -> Webhook:
        """Registers a webhook."""
        if events is None:
            events = []
        raw = json.dumps(events)
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO webhooks (project_id, url, events, secret)
            VALUES ($1,$2,$3,$4)
            RETURNING {WEBHOOK_COLUMNS}
            """,
            project_id,
            url,
            raw,
            secret,
        )
        return scan_webhook(row)

    async def delete(self, project_id: uuid.UUID, id: uuid.UUID) -> None:
        """Removes a webhook scoped to its project."""
        result = await self.pool.execute(
            "DELETE FROM webhooks WHERE id=$1 AND project_id=$2", id, project_id
        )
        if result.split()[-1] == "0":
            raise NotFoundError

    async def record_delivery(self, id: uuid.UUID, status: int, err_msg: str) -> None:
        """Stores the outcome of the last delivery attempt."""
        err_ptr = err_msg if err_msg else None
        try:
            await self.pool.execute(
                """
                UPDATE webhooks SET last_status=$2, last_error=$3, last_sent_at=now() WHERE id=$1
                """,
                id,
                status,
                err_ptr,
            )
        except Exception:
            # Fire-and-forget, mirroring the Go version's ignored error.
            pass