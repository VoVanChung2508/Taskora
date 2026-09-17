"""
Audit store — Python port of the Go `store.AuditStore` type.

Records security-relevant events for compliance review, using asyncpg for
database access.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit action names. Keeping them as constants avoids typos drifting between
# call sites and makes the set greppable.
# ---------------------------------------------------------------------------

AUDIT_LOGIN = "auth.login"
AUDIT_LOGOUT = "auth.logout"
AUDIT_MFA_ENABLED = "auth.mfa_enabled"
AUDIT_MFA_DISABLED = "auth.mfa_disabled"
AUDIT_MFA_VERIFIED = "auth.mfa_verified"
AUDIT_SESSION_REVOKED = "auth.session_revoked"
AUDIT_ROLE_CHANGED = "iam.role_changed"
AUDIT_CUSTOM_ROLE_SET = "iam.custom_role_assigned"
AUDIT_MEMBER_ADDED = "iam.member_added"
AUDIT_API_KEY_CREATED = "apikey.created"
AUDIT_API_KEY_REVOKED = "apikey.revoked"
AUDIT_DATA_EXPORTED = "privacy.data_exported"
AUDIT_ACCOUNT_ERASED = "privacy.account_erased"
AUDIT_WORKSPACE_DEL = "workspace.deleted"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

@dataclass
class AuditEntryInput:
    """Carries the fields of one audit record to be written (input side)."""

    action: str
    actor_id: Optional[uuid.UUID] = None
    actor_email: str = ""
    workspace_id: Optional[uuid.UUID] = None
    target: str = ""
    ip: str = ""
    meta: Optional[dict[str, Any]] = None


@dataclass
class AuditEntry:
    """Equivalent of `domain.AuditEntry` — a stored audit record (read side)."""

    id: uuid.UUID
    actor_id: Optional[uuid.UUID]
    actor_email: str
    workspace_id: Optional[uuid.UUID]
    action: str
    target: str
    ip: str
    created_at: datetime
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AuditStore
# ---------------------------------------------------------------------------

class AuditStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def record(self, e: AuditEntryInput) -> None:
        """
        Write an audit row.

        Failures are swallowed: auditing must never break the user's
        request, and the error is not actionable at the call site.
        """
        meta = e.meta if e.meta is not None else {}
        raw = json.dumps(meta)

        try:
            await self.pool.execute(
                """
                INSERT INTO audit_log (actor_id, actor_email, workspace_id, action, target, ip, meta)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                e.actor_id,
                e.actor_email,
                e.workspace_id,
                e.action,
                e.target,
                e.ip,
                raw,
            )
        except Exception:
            # Intentionally swallowed — see docstring.
            logger.exception("audit: failed to record entry action=%s", e.action)

    async def list(
        self,
        workspace_id: Optional[uuid.UUID],
        limit: int,
    ) -> list[AuditEntry]:
        """Return recent audit entries, optionally scoped to a workspace."""
        if limit <= 0 or limit > 500:
            limit = 200

        rows = await self.pool.fetch(
            """
            SELECT a.id, a.actor_id, a.actor_email, a.workspace_id, a.action, a.target, a.ip, a.meta, a.created_at
            FROM audit_log a
            WHERE ($1::uuid IS NULL OR a.workspace_id = $1)
            ORDER BY a.created_at DESC
            LIMIT $2
            """,
            workspace_id,
            limit,
        )

        out: list[AuditEntry] = []
        for row in rows:
            meta_raw = row["meta"]
            meta: dict[str, Any] = {}
            if meta_raw:
                if isinstance(meta_raw, dict):
                    meta = meta_raw  # asyncpg may already decode jsonb via a codec
                else:
                    meta = json.loads(meta_raw)

            out.append(
                AuditEntry(
                    id=row["id"],
                    actor_id=row["actor_id"],
                    actor_email=row["actor_email"],
                    workspace_id=row["workspace_id"],
                    action=row["action"],
                    target=row["target"],
                    ip=row["ip"],
                    created_at=row["created_at"],
                    meta=meta,
                )
            )
        return out