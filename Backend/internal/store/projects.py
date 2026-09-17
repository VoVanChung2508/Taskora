"""
Project store — Python port of the Go `store.ProjectStore` type.

Handles persistence for projects and memberships, using asyncpg for
database access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Union

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")

# Columns shared by every SELECT / RETURNING clause below.
PROJECT_COLUMNS = (
    "id, workspace_id, portfolio_id, name, key, description, status, "
    "sharepoint_folder_path, sharepoint_item_id, start_date, end_date, "
    "created_by, created_at, updated_at"
)


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.Project` type)
# ---------------------------------------------------------------------------

@dataclass
class Project:
    id: uuid.UUID
    workspace_id: uuid.UUID
    portfolio_id: Optional[uuid.UUID]
    name: str
    key: str
    description: str
    status: str
    sharepoint_folder_path: Optional[str]
    sharepoint_item_id: Optional[str]
    start_date: Optional[date]
    end_date: Optional[date]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Row scanning helper (equivalent of the Go `scanProject` function)
# ---------------------------------------------------------------------------

def _scan_project(row: Optional[Union[asyncpg.Record, tuple]]) -> Project:
    """
    Build a Project from a row returned in PROJECT_COLUMNS order.

    Raises ErrNotFound when `row` is None (asyncpg's equivalent of
    `pgx.ErrNoRows` — `fetchrow` returns None rather than raising for a
    missing row).
    """
    if row is None:
        raise ErrNotFound

    (
        project_id,
        workspace_id,
        portfolio_id,
        name,
        key,
        description,
        status,
        sharepoint_folder_path,
        sharepoint_item_id,
        start_date,
        end_date,
        created_by,
        created_at,
        updated_at,
    ) = row

    return Project(
        id=project_id,
        workspace_id=workspace_id,
        portfolio_id=portfolio_id,
        name=name,
        key=key,
        description=description,
        status=status,
        sharepoint_folder_path=sharepoint_folder_path,
        sharepoint_item_id=sharepoint_item_id,
        start_date=start_date,
        end_date=end_date,
        created_by=created_by,
        created_at=created_at,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# CreateProjectParams
# ---------------------------------------------------------------------------

@dataclass
class CreateProjectParams:
    """Carries the inputs required to create a project."""

    workspace_id: uuid.UUID
    name: str
    key: str
    description: str
    created_by: uuid.UUID


# ---------------------------------------------------------------------------
# ProjectStore
# ---------------------------------------------------------------------------

class ProjectStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, p: CreateProjectParams) -> Project:
        """Insert a project and add the creator as manager in one transaction."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO projects (workspace_id, name, key, description, created_by)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING {PROJECT_COLUMNS}
                    """,
                    p.workspace_id,
                    p.name,
                    p.key,
                    p.description,
                    p.created_by,
                )
                proj = _scan_project(row)

                await conn.execute(
                    """
                    INSERT INTO project_members (project_id, user_id, role)
                    VALUES ($1, $2, 'manager')
                    """,
                    proj.id,
                    p.created_by,
                )

                return proj

    async def set_sharepoint_folder(self, project_id: uuid.UUID, path: str, item_id: str) -> None:
        """Record the synced SharePoint folder path/item id."""
        await self.pool.execute(
            """
            UPDATE projects SET sharepoint_folder_path = $2, sharepoint_item_id = $3
            WHERE id = $1
            """,
            project_id,
            path,
            item_id,
        )

    async def get_by_id(self, project_id: uuid.UUID) -> Project:
        """Fetch a project by id."""
        row = await self.pool.fetchrow(
            f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = $1",
            project_id,
        )
        return _scan_project(row)

    async def list_by_workspace(self, workspace_id: uuid.UUID) -> list[Project]:
        """Return active + archived projects in a workspace."""
        rows = await self.pool.fetch(
            f"""
            SELECT {PROJECT_COLUMNS} FROM projects
            WHERE workspace_id = $1 ORDER BY created_at DESC
            """,
            workspace_id,
        )
        return [_scan_project(row) for row in rows]