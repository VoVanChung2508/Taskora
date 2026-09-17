import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


WORKSPACE_COLUMNS = (
    "id, name, slug, sharepoint_folder_path, sharepoint_item_id, "
    "created_by, created_at, updated_at"
)

# WorkspaceRole is a plain string type alias in this port (e.g. "owner",
# "admin", "member") — mirrors domain.WorkspaceRole from Go.
WorkspaceRole = str


def prefix_cols(alias: str, columns: str) -> str:
    """Prefixes each comma-separated column name with a table alias, e.g.
    prefix_cols("w", "id, name") -> "w.id, w.name"."""
    return ", ".join(f"{alias}.{c.strip()}" for c in columns.split(","))


@dataclass
class Workspace:
    id: uuid.UUID
    name: str
    slug: str
    sharepoint_folder_path: Optional[str]
    sharepoint_item_id: Optional[str]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass
class MemberInfo:
    user_id: uuid.UUID
    display_name: str
    email: str
    avatar_url: Optional[str] = None
    role: WorkspaceRole = ""
    hourly_rate: float = 0.0
    currency: str = "USD"


def scan_workspace(row: Optional[asyncpg.Record]) -> Workspace:
    if row is None:
        raise NotFoundError
    return Workspace(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        sharepoint_folder_path=row["sharepoint_folder_path"],
        sharepoint_item_id=row["sharepoint_item_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class WorkspaceStore:
    """Handles persistence for workspaces and memberships."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, name: str, slug: str, created_by: uuid.UUID) -> Workspace:
        """Inserts a workspace and adds the creator as owner in one
        transaction."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO workspaces (name, slug, created_by)
                    VALUES ($1, $2, $3)
                    RETURNING {WORKSPACE_COLUMNS}
                    """,
                    name,
                    slug,
                    created_by,
                )
                ws = scan_workspace(row)

                await conn.execute(
                    """
                    INSERT INTO workspace_members (workspace_id, user_id, role)
                    VALUES ($1, $2, 'owner')
                    """,
                    ws.id,
                    created_by,
                )

                return ws

    async def set_sharepoint_folder(self, id: uuid.UUID, path: str, item_id: str) -> None:
        """Records the synced SharePoint folder path/item id."""
        await self.pool.execute(
            """
            UPDATE workspaces SET sharepoint_folder_path = $2, sharepoint_item_id = $3
            WHERE id = $1
            """,
            id,
            path,
            item_id,
        )

    async def get_by_id(self, id: uuid.UUID) -> Workspace:
        """Fetches a workspace by id."""
        row = await self.pool.fetchrow(
            f"SELECT {WORKSPACE_COLUMNS} FROM workspaces WHERE id = $1", id
        )
        return scan_workspace(row)

    async def list_for_user(self, user_id: uuid.UUID) -> list[Workspace]:
        """Returns workspaces the user is a member of."""
        rows = await self.pool.fetch(
            f"""
            SELECT {prefix_cols("w", WORKSPACE_COLUMNS)}
            FROM workspaces w
            JOIN workspace_members m ON m.workspace_id = w.id
            WHERE m.user_id = $1
            ORDER BY w.created_at DESC
            """,
            user_id,
        )
        return [scan_workspace(row) for row in rows]

    async def list_members(self, workspace_id: uuid.UUID) -> list[MemberInfo]:
        """Returns a workspace's members with profile + billing rate."""
        rows = await self.pool.fetch(
            """
            SELECT u.id, u.display_name, u.email::text, u.avatar_url, m.role,
                   COALESCE(r.hourly_rate, 0), COALESCE(r.currency, 'USD')
            FROM workspace_members m
            JOIN users u ON u.id = m.user_id
            LEFT JOIN user_rates r ON r.user_id = u.id
            WHERE m.workspace_id = $1
            ORDER BY u.display_name
            """,
            workspace_id,
        )
        return [
            MemberInfo(
                user_id=r[0],
                display_name=r[1],
                email=r[2],
                avatar_url=r[3],
                role=r[4],
                hourly_rate=r[5],
                currency=r[6],
            )
            for r in rows
        ]

    async def add_member_by_email(
        self, workspace_id: uuid.UUID, email: str, role: WorkspaceRole
    ) -> MemberInfo:
        """Adds an existing user (looked up by email) to a workspace.
        Raises NotFoundError if no user with that email exists yet."""
        row = await self.pool.fetchrow(
            "SELECT id, display_name, email::text FROM users WHERE email = $1", email
        )
        if row is None:
            raise NotFoundError

        user_id, name, mail = row[0], row[1], row[2]

        await self.pool.execute(
            """
            INSERT INTO workspace_members (workspace_id, user_id, role)
            VALUES ($1, $2, $3)
            ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role
            """,
            workspace_id,
            user_id,
            role,
        )

        return MemberInfo(
            user_id=user_id,
            display_name=name,
            email=mail,
            role=role,
            currency="USD",
        )

    async def update_member_role(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID, role: WorkspaceRole
    ) -> None:
        """Changes a member's workspace role."""
        await self.pool.execute(
            "UPDATE workspace_members SET role=$3 WHERE workspace_id=$1 AND user_id=$2",
            workspace_id,
            user_id,
            role,
        )

    async def set_rate(self, user_id: uuid.UUID, rate: float, currency: str) -> None:
        """Upserts a user's hourly billing rate."""
        await self.pool.execute(
            """
            INSERT INTO user_rates (user_id, hourly_rate, currency, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (user_id) DO UPDATE SET hourly_rate=EXCLUDED.hourly_rate, currency=EXCLUDED.currency, updated_at=now()
            """,
            user_id,
            rate,
            currency,
        )

    async def role_for_user(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> WorkspaceRole:
        """Returns the user's workspace role, or raises NotFoundError if not
        a member."""
        role = await self.pool.fetchval(
            "SELECT role FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            workspace_id,
            user_id,
        )
        if role is None:
            raise NotFoundError
        return role

    async def list_all(self) -> list[Workspace]:
        """Returns all workspaces in the system."""
        rows = await self.pool.fetch(
            f"SELECT {WORKSPACE_COLUMNS} FROM workspaces ORDER BY created_at DESC"
        )
        return [scan_workspace(row) for row in rows]

    async def delete(self, id: uuid.UUID) -> None:
        """Permanently removes a workspace and all its cascade
        dependencies."""
        await self.pool.execute("DELETE FROM workspaces WHERE id = $1", id)