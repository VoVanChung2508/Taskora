"""
Workspace store (custom roles + teams) — Python port of the Go
`store.WorkspaceStore` methods for Module 1.2 (custom roles) and
Module 1.3 (teams/departments).

Uses asyncpg for database access. `permissions` is stored as a native
Postgres array column (matching the Go code, which passes `[]domain.Permission`
straight through to pgx without JSON-encoding it), so Python lists of
strings are passed directly as query parameters — asyncpg encodes them as
a Postgres array automatically.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")

# Permission is a plain string in this port (equivalent of `domain.Permission`,
# assumed to be a Go string-based type / Postgres enum/text array element).
Permission = str


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

@dataclass
class CustomRole:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    created_at: datetime
    permissions: list[Permission] = field(default_factory=list)


@dataclass
class TeamMember:
    user_id: uuid.UUID
    display_name: str
    email: str


@dataclass
class Team:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    created_at: datetime
    members: list[TeamMember] = field(default_factory=list)


# ---------------------------------------------------------------------------
# WorkspaceStore
# ---------------------------------------------------------------------------

class WorkspaceStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ── Custom roles (Module 1.2) ─────────────────────────────────

    async def list_custom_roles(self, workspace_id: uuid.UUID) -> list[CustomRole]:
        """Return a workspace's custom roles."""
        rows = await self.pool.fetch(
            """
            SELECT id, workspace_id, name, permissions, created_at
            FROM custom_roles WHERE workspace_id = $1 ORDER BY created_at
            """,
            workspace_id,
        )

        out: list[CustomRole] = []
        for row in rows:
            r = CustomRole(
                id=row["id"],
                workspace_id=row["workspace_id"],
                name=row["name"],
                created_at=row["created_at"],
                permissions=list(row["permissions"]) if row["permissions"] else [],
            )
            out.append(r)
        return out

    async def create_custom_role(
        self,
        workspace_id: uuid.UUID,
        name: str,
        perms: Optional[list[Permission]],
    ) -> CustomRole:
        """Add a role with an explicit permission set."""
        perms = perms or []

        row = await self.pool.fetchrow(
            """
            INSERT INTO custom_roles (workspace_id, name, permissions)
            VALUES ($1, $2, $3)
            RETURNING id, workspace_id, name, permissions, created_at
            """,
            workspace_id,
            name,
            perms,
        )

        return CustomRole(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            created_at=row["created_at"],
            permissions=list(row["permissions"]) if row["permissions"] else [],
        )

    async def update_custom_role(
        self,
        workspace_id: uuid.UUID,
        role_id: uuid.UUID,
        name: str,
        perms: Optional[list[Permission]],
    ) -> None:
        """Replace a role's name and permission set."""
        perms = perms or []

        result = await self.pool.execute(
            """
            UPDATE custom_roles SET name = $3, permissions = $4
            WHERE id = $2 AND workspace_id = $1
            """,
            workspace_id,
            role_id,
            name,
            perms,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def delete_custom_role(self, workspace_id: uuid.UUID, role_id: uuid.UUID) -> None:
        """
        Remove a role; members holding it fall back to their built-in
        workspace role (the FK is ON DELETE SET NULL).
        """
        result = await self.pool.execute(
            "DELETE FROM custom_roles WHERE id = $2 AND workspace_id = $1",
            workspace_id,
            role_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def assign_custom_role(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        role_id: Optional[uuid.UUID],
    ) -> None:
        """
        Attach (or clear, when role_id is None) a custom role on a
        workspace member.
        """
        result = await self.pool.execute(
            """
            UPDATE workspace_members SET custom_role_id = $3
            WHERE workspace_id = $1 AND user_id = $2
            """,
            workspace_id,
            user_id,
            role_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def permissions_for_user(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Optional[list[Permission]]:
        """
        Return the permission set granted by the member's custom role.

        Members without a custom role return None, meaning "fall back to
        the built-in role checks".
        """
        row = await self.pool.fetchrow(
            """
            SELECT r.permissions
            FROM workspace_members m
            JOIN custom_roles r ON r.id = m.custom_role_id
            WHERE m.workspace_id = $1 AND m.user_id = $2
            """,
            workspace_id,
            user_id,
        )
        if row is None:
            return None
        perms = row["permissions"]
        return list(perms) if perms else perms

    # ── Teams / departments (Module 1.3) ──────────────────────────

    async def list_teams(self, workspace_id: uuid.UUID) -> list[Team]:
        """Return a workspace's teams with their members."""
        rows = await self.pool.fetch(
            """
            SELECT id, workspace_id, name, created_at
            FROM teams WHERE workspace_id = $1 ORDER BY name
            """,
            workspace_id,
        )

        teams: list[Team] = []
        index: dict[uuid.UUID, int] = {}
        for row in rows:
            t = Team(
                id=row["id"],
                workspace_id=row["workspace_id"],
                name=row["name"],
                created_at=row["created_at"],
            )
            index[t.id] = len(teams)
            teams.append(t)

        if not teams:
            return teams

        # Attach members in one query.
        mrows = await self.pool.fetch(
            """
            SELECT tm.team_id, u.id, u.display_name, u.email::text
            FROM team_members tm
            JOIN teams t ON t.id = tm.team_id
            JOIN users u ON u.id = tm.user_id
            WHERE t.workspace_id = $1
            ORDER BY u.display_name
            """,
            workspace_id,
        )
        for team_id, user_id, display_name, email in mrows:
            i = index.get(team_id)
            if i is not None:
                teams[i].members.append(
                    TeamMember(user_id=user_id, display_name=display_name, email=email)
                )

        return teams

    async def create_team(self, workspace_id: uuid.UUID, name: str) -> Team:
        """Add a team to a workspace."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO teams (workspace_id, name) VALUES ($1, $2)
            RETURNING id, workspace_id, name, created_at
            """,
            workspace_id,
            name,
        )
        return Team(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            created_at=row["created_at"],
        )

    async def delete_team(self, workspace_id: uuid.UUID, team_id: uuid.UUID) -> None:
        """Remove a team (memberships cascade)."""
        result = await self.pool.execute(
            "DELETE FROM teams WHERE id = $2 AND workspace_id = $1",
            workspace_id,
            team_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def set_team_member(
        self,
        workspace_id: uuid.UUID,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        on: bool,
    ) -> None:
        """
        Add or remove a user from a team.

        The user must already be a member of the team's workspace.
        """
        if not on:
            await self.pool.execute(
                "DELETE FROM team_members WHERE team_id = $1 AND user_id = $2",
                team_id,
                user_id,
            )
            return

        result = await self.pool.execute(
            """
            INSERT INTO team_members (team_id, user_id)
            SELECT t.id, m.user_id
            FROM teams t
            JOIN workspace_members m ON m.workspace_id = t.workspace_id AND m.user_id = $3
            WHERE t.id = $2 AND t.workspace_id = $1
            ON CONFLICT DO NOTHING
            """,
            workspace_id,
            team_id,
            user_id,
        )
        if _rows_affected(result) == 0:
            # Either the team is not in this workspace or the user is not a member.
            raise ErrNotFound


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'UPDATE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0