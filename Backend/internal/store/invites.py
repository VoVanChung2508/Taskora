"""
Invite store — Python port of the Go `store.InviteStore` type.

Persists workspace invitations, using asyncpg for database access.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


class InviteEmailMismatchError(Exception):
    """Equivalent of the Go `ErrInviteEmailMismatch` sentinel error."""


ErrNotFound = NotFoundError("not found")
ErrInviteEmailMismatch = InviteEmailMismatchError("invite email does not match")

# WorkspaceRole is a plain string in this port (equivalent of
# `domain.WorkspaceRole`, assumed to be a Go string-based type / Postgres
# enum).
WorkspaceRole = str


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.WorkspaceInvite` type)
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceInvite:
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    role: WorkspaceRole
    invited_by: uuid.UUID
    expires_at: datetime
    accepted_at: Optional[datetime]
    created_at: datetime
    expired: bool = False


# ---------------------------------------------------------------------------
# Free functions (equivalent of the Go package-level helpers)
# ---------------------------------------------------------------------------

def generate_invite_token() -> str:
    """Return the secret handed to the invitee."""
    buf = os.urandom(24)
    return urlsafe_b64encode(buf).rstrip(b"=").decode("ascii")


def hash_invite_token(token: str) -> str:
    """Return the digest stored in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# InviteStore
# ---------------------------------------------------------------------------

class InviteStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        workspace_id: uuid.UUID,
        email: str,
        role: WorkspaceRole,
        invited_by: uuid.UUID,
        token: str,
        ttl: timedelta,
    ) -> WorkspaceInvite:
        """Issue an invite, replacing any pending one for the same address."""
        email = email.strip().lower()

        # Re-inviting someone should refresh the invite rather than fail on
        # the pending-unique index.
        await self.pool.execute(
            """
            DELETE FROM workspace_invites
            WHERE workspace_id = $1 AND lower(email) = $2 AND accepted_at IS NULL
            """,
            workspace_id,
            email,
        )

        row = await self.pool.fetchrow(
            """
            INSERT INTO workspace_invites (workspace_id, email, role, token_hash, invited_by, expires_at)
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING id, workspace_id, email, role, invited_by, expires_at, accepted_at, created_at
            """,
            workspace_id,
            email,
            role,
            hash_invite_token(token),
            invited_by,
            datetime.now(timezone.utc) + ttl,
        )

        return WorkspaceInvite(
            id=row["id"],
            workspace_id=row["workspace_id"],
            email=row["email"],
            role=row["role"],
            invited_by=row["invited_by"],
            expires_at=row["expires_at"],
            accepted_at=row["accepted_at"],
            created_at=row["created_at"],
        )

    async def list_pending(self, workspace_id: uuid.UUID) -> list[WorkspaceInvite]:
        """Return a workspace's outstanding invites."""
        rows = await self.pool.fetch(
            """
            SELECT id, workspace_id, email, role, invited_by, expires_at, accepted_at, created_at
            FROM workspace_invites
            WHERE workspace_id = $1 AND accepted_at IS NULL
            ORDER BY created_at DESC
            """,
            workspace_id,
        )

        now = datetime.now(timezone.utc)
        out: list[WorkspaceInvite] = []
        for row in rows:
            expires_at = row["expires_at"]
            in_ = WorkspaceInvite(
                id=row["id"],
                workspace_id=row["workspace_id"],
                email=row["email"],
                role=row["role"],
                invited_by=row["invited_by"],
                expires_at=expires_at,
                accepted_at=row["accepted_at"],
                created_at=row["created_at"],
            )
            in_.expired = now > expires_at
            out.append(in_)
        return out

    async def revoke(self, workspace_id: uuid.UUID, invite_id: uuid.UUID) -> None:
        """Remove a pending invite."""
        result = await self.pool.execute(
            """
            DELETE FROM workspace_invites
            WHERE id=$1 AND workspace_id=$2 AND accepted_at IS NULL
            """,
            invite_id,
            workspace_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def accept(
        self,
        token: str,
        user_id: uuid.UUID,
        user_email: str,
    ) -> WorkspaceInvite:
        """
        Redeem a token for a user, adding them to the workspace.

        The invite is bound to the email it was sent to, so forwarding the
        link to someone else does not grant them access.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, workspace_id, email, role, invited_by, expires_at, accepted_at, created_at
                    FROM workspace_invites
                    WHERE token_hash = $1 AND accepted_at IS NULL AND expires_at > now()
                    FOR UPDATE
                    """,
                    hash_invite_token(token),
                )
                if row is None:
                    raise ErrNotFound

                in_ = WorkspaceInvite(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    email=row["email"],
                    role=row["role"],
                    invited_by=row["invited_by"],
                    expires_at=row["expires_at"],
                    accepted_at=row["accepted_at"],
                    created_at=row["created_at"],
                )

                if in_.email.strip().lower() != user_email.strip().lower():
                    raise ErrInviteEmailMismatch

                await conn.execute(
                    """
                    INSERT INTO workspace_members (workspace_id, user_id, role)
                    VALUES ($1,$2,$3)
                    ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role
                    """,
                    in_.workspace_id,
                    user_id,
                    in_.role,
                )
                await conn.execute(
                    "UPDATE workspace_invites SET accepted_at = now() WHERE id = $1",
                    in_.id,
                )

                return in_


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0