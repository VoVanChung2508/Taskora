import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a requested row does not exist or is not accessible."""
    pass


@dataclass
class UserSession:
    id: uuid.UUID
    device: str
    ip: str
    last_seen: datetime
    expires_at: datetime
    created_at: datetime
    current: bool = False


class SessionStore:
    """Persists issued sessions so they can be listed and revoked
    remotely (Module 1.1)."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        user_id: uuid.UUID,
        token_hash: str,
        device: str,
        ip: str,
        expires_at: datetime,
    ) -> None:
        """Records a newly issued session. token_hash must be a digest of the
        token — the raw token is never stored."""
        await self.pool.execute(
            """
            INSERT INTO user_sessions (user_id, token_hash, device, ip, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (token_hash) DO UPDATE
                SET last_seen = now(), revoked_at = NULL, expires_at = EXCLUDED.expires_at
            """,
            user_id,
            token_hash,
            device,
            ip,
            expires_at,
        )

    async def is_revoked(self, token_hash: str) -> bool:
        """Reports whether a known session has been revoked. Unknown tokens
        return False so sessions issued before this feature keep working."""
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(revoked_at IS NOT NULL, false) AS revoked
            FROM user_sessions WHERE token_hash = $1
            """,
            token_hash,
        )
        if row is None:
            # No row → not a tracked session; treat as active.
            return False
        return row["revoked"]

    async def touch(self, token_hash: str) -> None:
        """Refreshes last_seen, throttled to at most once every 5 minutes so
        an active session does not write on every request."""
        try:
            await self.pool.execute(
                """
                UPDATE user_sessions SET last_seen = now()
                WHERE token_hash = $1 AND last_seen < now() - interval '5 minutes'
                """,
                token_hash,
            )
        except Exception:
            # Fire-and-forget, mirroring the Go version's ignored error.
            pass

    async def list_for_user(self, user_id: uuid.UUID) -> list[UserSession]:
        """Returns a user's active (non-expired, non-revoked) sessions."""
        rows = await self.pool.fetch(
            """
            SELECT id, device, ip, last_seen, expires_at, created_at
            FROM user_sessions
            WHERE user_id = $1 AND revoked_at IS NULL AND expires_at > now()
            ORDER BY last_seen DESC
            """,
            user_id,
        )
        return [
            UserSession(
                id=row["id"],
                device=row["device"],
                ip=row["ip"],
                last_seen=row["last_seen"],
                expires_at=row["expires_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def revoke(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """Marks one of the user's sessions as revoked."""
        result = await self.pool.execute(
            """
            UPDATE user_sessions SET revoked_at = now()
            WHERE id = $2 AND user_id = $1 AND revoked_at IS NULL
            """,
            user_id,
            session_id,
        )
        if result.split()[-1] == "0":
            raise NotFoundError

    async def revoke_by_token(self, token_hash: str) -> None:
        """Marks the session matching a token hash as revoked (logout)."""
        await self.pool.execute(
            "UPDATE user_sessions SET revoked_at = now() WHERE token_hash = $1",
            token_hash,
        )

    async def rotate(
        self,
        user_id: uuid.UUID,
        old_hash: str,
        new_hash: str,
        expires_at: datetime,
    ) -> None:
        """Retires a session token and issues a row for its replacement.

        The old row is *marked revoked* rather than updated in place:
        is_revoked treats an unknown hash as valid (so sessions predating
        device tracking keep working), which means simply overwriting the
        hash would leave the old token silently usable until its JWT expiry.
        Stamping revoked_at is what actually kills it.

        Raises NotFoundError when the old hash is unknown or already revoked,
        so a replayed token cannot mint a fresh session.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE user_sessions SET revoked_at = now()
                    WHERE token_hash = $1 AND user_id = $2 AND revoked_at IS NULL
                    RETURNING device, ip
                    """,
                    old_hash,
                    user_id,
                )
                if row is None:
                    raise NotFoundError

                device, ip = row["device"], row["ip"]

                # Carry the device/IP across so the account page still shows
                # one entry.
                await conn.execute(
                    """
                    INSERT INTO user_sessions (user_id, token_hash, device, ip, expires_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (token_hash) DO UPDATE
                        SET revoked_at = NULL, expires_at = EXCLUDED.expires_at, last_seen = now()
                    """,
                    user_id,
                    new_hash,
                    device,
                    ip,
                    expires_at,
                )

    async def id_by_token(self, token_hash: str) -> uuid.UUID:
        """Resolves the session row matching a token hash, so the caller's
        own session can be labelled in the device list."""
        row = await self.pool.fetchrow(
            "SELECT id FROM user_sessions WHERE token_hash = $1", token_hash
        )
        if row is None:
            raise NotFoundError
        return row["id"]


def mark_current(sessions: list[UserSession], current_id: uuid.UUID) -> None:
    """Flags which of the listed sessions matches the caller's token."""
    for session in sessions:
        if session.id == current_id:
            session.current = True