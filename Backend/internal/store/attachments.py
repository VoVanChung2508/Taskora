"""
Attachment store — Python port of the Go `store.AttachmentStore` type.

Records metadata for files stored in SharePoint, using asyncpg for
database access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")


# Columns shared by ListByTask and Create's RETURNING clause.
ATTACHMENT_COLUMNS = (
    "a.id, a.task_id, a.uploaded_by, a.name, a.size_bytes, a.content_type, "
    "a.drive_item_id, a.web_url, a.folder_path, a.created_at"
)


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.Attachment` type)
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    id: Optional[uuid.UUID]
    task_id: uuid.UUID
    uploaded_by: Optional[uuid.UUID]
    name: str
    size_bytes: int
    content_type: str
    drive_item_id: str
    web_url: str
    folder_path: str
    created_at: Optional[datetime] = None
    uploader_name: str = ""


# ---------------------------------------------------------------------------
# AttachmentStore
# ---------------------------------------------------------------------------

class AttachmentStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_by_task(self, task_id: uuid.UUID) -> list[Attachment]:
        """Return a task's attachments, newest first."""
        rows = await self.pool.fetch(
            f"""
            SELECT {ATTACHMENT_COLUMNS}, COALESCE(u.display_name, u.email::text, '')
            FROM attachments a
            LEFT JOIN users u ON u.id = a.uploaded_by
            WHERE a.task_id = $1
            ORDER BY a.created_at DESC
            """,
            task_id,
        )

        out: list[Attachment] = []
        for row in rows:
            out.append(
                Attachment(
                    id=row[0],
                    task_id=row[1],
                    uploaded_by=row[2],
                    name=row[3],
                    size_bytes=row[4],
                    content_type=row[5],
                    drive_item_id=row[6],
                    web_url=row[7],
                    folder_path=row[8],
                    created_at=row[9],
                    uploader_name=row[10],
                )
            )
        return out

    async def create(self, a: Attachment) -> Attachment:
        """Record an uploaded file."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO attachments (task_id, uploaded_by, name, size_bytes, content_type, drive_item_id, web_url, folder_path)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id, task_id, uploaded_by, name, size_bytes, content_type, drive_item_id, web_url, folder_path, created_at
            """,
            a.task_id,
            a.uploaded_by,
            a.name,
            a.size_bytes,
            a.content_type,
            a.drive_item_id,
            a.web_url,
            a.folder_path,
        )

        return Attachment(
            id=row["id"],
            task_id=row["task_id"],
            uploaded_by=row["uploaded_by"],
            name=row["name"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            drive_item_id=row["drive_item_id"],
            web_url=row["web_url"],
            folder_path=row["folder_path"],
            created_at=row["created_at"],
        )

    async def delete(self, task_id: uuid.UUID, attachment_id: uuid.UUID) -> None:
        """Remove an attachment record for a task."""
        result = await self.pool.execute(
            "DELETE FROM attachments WHERE id=$1 AND task_id=$2",
            attachment_id,
            task_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0