"""
Report store — Python port of the Go `store.ReportStore` type.

Persists scheduled report definitions, using asyncpg for database access.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union

import asyncpg

logger = logging.getLogger(__name__)


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")

# Columns shared by every SELECT / RETURNING clause below.
REPORT_COLUMNS = (
    "id, workspace_id, project_id, name, frequency, channel_url, provider, "
    "hour_utc, active, last_run_at, last_status, last_error, created_at"
)


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.ScheduledReport` type)
# ---------------------------------------------------------------------------

@dataclass
class ScheduledReport:
    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: Optional[uuid.UUID]
    name: str
    frequency: str
    channel_url: str
    provider: str
    hour_utc: int
    active: bool
    last_run_at: Optional[datetime]
    last_status: Optional[int]
    last_error: Optional[str]
    created_at: datetime


# ---------------------------------------------------------------------------
# Row scanning helper (equivalent of the Go `scanReport` function)
# ---------------------------------------------------------------------------

def _scan_report(row: Union[asyncpg.Record, tuple]) -> ScheduledReport:
    """Build a ScheduledReport from a row returned in REPORT_COLUMNS order."""
    (
        report_id,
        workspace_id,
        project_id,
        name,
        frequency,
        channel_url,
        provider,
        hour_utc,
        active,
        last_run_at,
        last_status,
        last_error,
        created_at,
    ) = row

    return ScheduledReport(
        id=report_id,
        workspace_id=workspace_id,
        project_id=project_id,
        name=name,
        frequency=frequency,
        channel_url=channel_url,
        provider=provider,
        hour_utc=hour_utc,
        active=active,
        last_run_at=last_run_at,
        last_status=last_status,
        last_error=last_error,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# ReportStore
# ---------------------------------------------------------------------------

class ReportStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_by_workspace(self, workspace_id: uuid.UUID) -> list[ScheduledReport]:
        """Return a workspace's scheduled reports."""
        rows = await self.pool.fetch(
            f"SELECT {REPORT_COLUMNS} FROM scheduled_reports WHERE workspace_id=$1 ORDER BY created_at",
            workspace_id,
        )
        return [_scan_report(row) for row in rows]

    async def create(
        self,
        workspace_id: uuid.UUID,
        project_id: Optional[uuid.UUID],
        name: str,
        frequency: str,
        channel_url: str,
        provider: str,
        hour_utc: int,
    ) -> ScheduledReport:
        """Add a scheduled report."""
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO scheduled_reports (workspace_id, project_id, name, frequency, channel_url, provider, hour_utc)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            RETURNING {REPORT_COLUMNS}
            """,
            workspace_id,
            project_id,
            name,
            frequency,
            channel_url,
            provider,
            hour_utc,
        )
        return _scan_report(row)

    async def delete(self, workspace_id: uuid.UUID, report_id: uuid.UUID) -> None:
        """Remove a report scoped to its workspace."""
        result = await self.pool.execute(
            "DELETE FROM scheduled_reports WHERE id=$1 AND workspace_id=$2",
            report_id,
            workspace_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def due_now(self, hour_utc: int) -> list[ScheduledReport]:
        """
        Return active reports whose send hour matches and that have not
        already run within their period.

        The "already ran" guard is what makes the scheduler safe to run
        every few minutes and safe against a restart: a daily report only
        fires once per day, a weekly one once per 7 days.
        """
        rows = await self.pool.fetch(
            f"""
            SELECT {REPORT_COLUMNS} FROM scheduled_reports
            WHERE active AND hour_utc = $1
              AND (
                last_run_at IS NULL
                OR (frequency = 'daily'  AND last_run_at < now() - interval '20 hours')
                OR (frequency = 'weekly' AND last_run_at < now() - interval '6 days')
              )
            """,
            hour_utc,
        )
        return [_scan_report(row) for row in rows]

    async def mark_run(self, report_id: uuid.UUID, status: int, err_msg: str) -> None:
        """
        Record the outcome of a delivery attempt.

        Failures are swallowed: recording the outcome must never break the
        caller, and the error is not actionable at the call site.
        """
        err_val = err_msg if err_msg else None
        try:
            await self.pool.execute(
                """
                UPDATE scheduled_reports SET last_run_at = now(), last_status = $2, last_error = $3
                WHERE id = $1
                """,
                report_id,
                status,
                err_val,
            )
        except Exception:
            # Intentionally swallowed — see docstring.
            logger.exception("report: failed to record run id=%s", report_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0