"""
API key store — Python port of the Go `store.APIKeyStore` type.

Uses asyncpg for database access. Plaintext keys are never stored: only a
SHA-256 hash and a short, non-secret prefix for display purposes.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")

# API_KEY_PREFIX marks Flowie keys so they are recognisable in logs and
# secret scanners.
API_KEY_PREFIX = "flw_"


# ---------------------------------------------------------------------------
# Domain model (equivalent of the referenced `domain.APIKey` type)
# ---------------------------------------------------------------------------

@dataclass
class APIKey:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str] = field(default_factory=list)
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    active: bool = True


@dataclass
class ResolvedKey:
    """What a valid API key grants."""

    key_id: uuid.UUID
    workspace_id: uuid.UUID
    scopes: list[str] = field(default_factory=list)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ---------------------------------------------------------------------------
# Free functions (equivalent of the Go package-level helpers)
# ---------------------------------------------------------------------------

def generate_api_key() -> str:
    """Return a new random key in plaintext.

    It is shown to the user once and never stored as-is.
    """
    buf = os.urandom(32)
    return API_KEY_PREFIX + urlsafe_b64encode(buf).rstrip(b"=").decode("ascii")


def hash_api_key(key: str) -> str:
    """Return the digest stored for a key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _visible_prefix(key: str) -> str:
    """Return the short, non-secret part shown in the UI."""
    cutoff = len(API_KEY_PREFIX) + 6
    if len(key) <= cutoff:
        return key
    return key[:cutoff]


# ---------------------------------------------------------------------------
# APIKeyStore
# ---------------------------------------------------------------------------

class APIKeyStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        workspace_id: uuid.UUID,
        created_by: uuid.UUID,
        name: str,
        scopes: Optional[list[str]],
        plaintext: str,
    ) -> APIKey:
        """Store a new key and return the record (without the plaintext)."""
        if not scopes:
            scopes = ["read"]
        raw = json.dumps(scopes)

        row = await self.pool.fetchrow(
            """
            INSERT INTO api_keys (workspace_id, created_by, name, prefix, key_hash, scopes)
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING id, workspace_id, name, prefix, scopes, last_used_at, revoked_at, created_at
            """,
            workspace_id,
            created_by,
            name,
            _visible_prefix(plaintext),
            hash_api_key(plaintext),
            raw,
        )

        k = APIKey(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            prefix=row["prefix"],
            scopes=_load_scopes(row["scopes"]),
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
        )
        k.active = k.revoked_at is None
        return k

    async def list_by_workspace(self, workspace_id: uuid.UUID) -> list[APIKey]:
        """Return a workspace's keys (never the secret)."""
        rows = await self.pool.fetch(
            """
            SELECT id, workspace_id, name, prefix, scopes, last_used_at, revoked_at, created_at
            FROM api_keys WHERE workspace_id=$1 ORDER BY created_at DESC
            """,
            workspace_id,
        )

        out: list[APIKey] = []
        for row in rows:
            k = APIKey(
                id=row["id"],
                workspace_id=row["workspace_id"],
                name=row["name"],
                prefix=row["prefix"],
                scopes=_load_scopes(row["scopes"]),
                last_used_at=row["last_used_at"],
                revoked_at=row["revoked_at"],
                created_at=row["created_at"],
            )
            k.active = k.revoked_at is None
            out.append(k)
        return out

    async def revoke(self, workspace_id: uuid.UUID, key_id: uuid.UUID) -> None:
        """Mark a key unusable."""
        result = await self.pool.execute(
            """
            UPDATE api_keys SET revoked_at = now()
            WHERE id=$1 AND workspace_id=$2 AND revoked_at IS NULL
            """,
            key_id,
            workspace_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def resolve(self, plaintext: str) -> ResolvedKey:
        """
        Look up an active key by its plaintext value and record usage.

        Raises NotFoundError for unknown or revoked keys.
        """
        plaintext = plaintext.strip()
        if not plaintext:
            raise ErrNotFound

        row = await self.pool.fetchrow(
            """
            SELECT id, workspace_id, scopes FROM api_keys
            WHERE key_hash = $1 AND revoked_at IS NULL
            """,
            hash_api_key(plaintext),
        )
        if row is None:
            raise ErrNotFound

        r = ResolvedKey(
            key_id=row["id"],
            workspace_id=row["workspace_id"],
            scopes=_load_scopes(row["scopes"]),
        )

        # Usage timestamp is throttled to avoid a write on every request.
        await self.pool.execute(
            """
            UPDATE api_keys SET last_used_at = now()
            WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
            """,
            r.key_id,
        )
        return r


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_scopes(raw) -> list[str]:
    """Decode the `scopes` JSON column, tolerating None/empty values."""
    if not raw:
        return []
    if isinstance(raw, (list, dict)):
        return raw  # asyncpg may already decode jsonb via a codec
    return json.loads(raw)


def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'UPDATE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0