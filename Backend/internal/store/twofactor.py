import hashlib
import json
import uuid
from dataclasses import dataclass

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


def hash_recovery_code(code: str) -> str:
    """Returns the digest stored for a backup code. Codes are never
    persisted in clear text."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass
class TwoFactorState:
    """Describes a user's MFA configuration."""

    secret: str
    enabled: bool
    recovery_len: int = 0


class UserStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def two_factor(self, user_id: uuid.UUID) -> TwoFactorState:
        """Returns the user's MFA state."""
        row = await self.pool.fetchrow(
            "SELECT totp_secret, totp_enabled, recovery_codes FROM users WHERE id=$1",
            user_id,
        )
        if row is None:
            raise NotFoundError

        secret, enabled, codes_raw = row["totp_secret"], row["totp_enabled"], row["recovery_codes"]

        codes_list: list[str] = []
        if codes_raw:
            try:
                codes_list = json.loads(codes_raw) if isinstance(codes_raw, str) else codes_raw
            except (ValueError, TypeError):
                codes_list = []

        return TwoFactorState(secret=secret, enabled=enabled, recovery_len=len(codes_list))

    async def start_totp_enrolment(self, user_id: uuid.UUID, secret: str) -> None:
        """Stores a pending secret without enabling MFA yet, so a failed
        enrolment cannot lock the user out."""
        await self.pool.execute(
            "UPDATE users SET totp_secret=$2, totp_enabled=FALSE WHERE id=$1",
            user_id,
            secret,
        )

    async def enable_totp(self, user_id: uuid.UUID, hashed_codes: list[str]) -> None:
        """Switches MFA on and stores hashed recovery codes."""
        raw = json.dumps(hashed_codes)
        await self.pool.execute(
            "UPDATE users SET totp_enabled=TRUE, recovery_codes=$2 WHERE id=$1",
            user_id,
            raw,
        )

    async def disable_totp(self, user_id: uuid.UUID) -> None:
        """Clears the secret and recovery codes."""
        await self.pool.execute(
            "UPDATE users SET totp_enabled=FALSE, totp_secret='', recovery_codes='[]' WHERE id=$1",
            user_id,
        )

    async def consume_recovery_code(self, user_id: uuid.UUID, code: str) -> bool:
        """Removes a matching backup code, returning True when one was
        spent. Codes are single-use."""
        raw = await self.pool.fetchval(
            "SELECT recovery_codes FROM users WHERE id=$1", user_id
        )

        codes: list[str] = []
        if raw:
            try:
                codes = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                codes = []

        want = hash_recovery_code(code)
        out: list[str] = []
        found = False
        for c in codes:
            if not found and c == want:
                found = True
                continue  # drop it: single use
            out.append(c)

        if not found:
            return False

        next_raw = json.dumps(out)
        await self.pool.execute(
            "UPDATE users SET recovery_codes=$2 WHERE id=$1", user_id, next_raw
        )
        return True