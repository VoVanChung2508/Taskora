"""
Dashboard store — Python port of the Go `store.DashboardStore` type.

Persists custom dashboards and their widgets, using asyncpg for database
access.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import asyncpg


class NotFoundError(Exception):
    """Equivalent of the Go `ErrNotFound` sentinel error."""


ErrNotFound = NotFoundError("not found")


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain` package types)
# ---------------------------------------------------------------------------

@dataclass
class DashboardWidget:
    id: uuid.UUID
    dashboard_id: uuid.UUID
    type: str
    title: str
    position: int
    width: int
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class Dashboard:
    id: uuid.UUID
    workspace_id: uuid.UUID
    owner_id: Optional[uuid.UUID]
    name: str
    created_at: datetime
    shared: bool = False
    widgets: list[DashboardWidget] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DashboardStore
# ---------------------------------------------------------------------------

class DashboardStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_for_user(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> list[Dashboard]:
        """
        Return the workspace's dashboards visible to a user: their own
        plus any shared (owner-less) ones.
        """
        rows = await self.pool.fetch(
            """
            SELECT id, workspace_id, owner_id, name, created_at
            FROM dashboards
            WHERE workspace_id = $1 AND (owner_id IS NULL OR owner_id = $2)
            ORDER BY created_at
            """,
            workspace_id,
            user_id,
        )

        out: list[Dashboard] = []
        index: dict[uuid.UUID, int] = {}
        for row in rows:
            d = Dashboard(
                id=row["id"],
                workspace_id=row["workspace_id"],
                owner_id=row["owner_id"],
                name=row["name"],
                created_at=row["created_at"],
            )
            d.shared = d.owner_id is None
            index[d.id] = len(out)
            out.append(d)

        if not out:
            return out

        # Attach widgets in one query.
        wrows = await self.pool.fetch(
            """
            SELECT w.id, w.dashboard_id, w.type, w.title, w.config, w.position, w.width
            FROM dashboard_widgets w
            JOIN dashboards d ON d.id = w.dashboard_id
            WHERE d.workspace_id = $1 AND (d.owner_id IS NULL OR d.owner_id = $2)
            ORDER BY w.position
            """,
            workspace_id,
            user_id,
        )

        for wrow in wrows:
            widget_id, dashboard_id, wtype, title, cfg_raw, position, width = (
                wrow[0], wrow[1], wrow[2], wrow[3], wrow[4], wrow[5], wrow[6]
            )
            wdg = DashboardWidget(
                id=widget_id,
                dashboard_id=dashboard_id,
                type=wtype,
                title=title,
                position=position,
                width=width,
                config=_decode_json_object(cfg_raw),
            )
            i = index.get(wdg.dashboard_id)
            if i is not None:
                out[i].widgets.append(wdg)

        return out

    async def create_dashboard(
        self,
        workspace_id: uuid.UUID,
        owner: Optional[uuid.UUID],
        name: str,
    ) -> Dashboard:
        """Add a dashboard. Pass owner=None to share it workspace-wide."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO dashboards (workspace_id, owner_id, name)
            VALUES ($1, $2, $3)
            RETURNING id, workspace_id, owner_id, name, created_at
            """,
            workspace_id,
            owner,
            name,
        )

        d = Dashboard(
            id=row["id"],
            workspace_id=row["workspace_id"],
            owner_id=row["owner_id"],
            name=row["name"],
            created_at=row["created_at"],
        )
        d.shared = d.owner_id is None
        return d

    async def delete_dashboard(self, workspace_id: uuid.UUID, dashboard_id: uuid.UUID) -> None:
        """Remove a dashboard within a workspace."""
        result = await self.pool.execute(
            "DELETE FROM dashboards WHERE id = $1 AND workspace_id = $2",
            dashboard_id,
            workspace_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound

    async def dashboard_workspace(self, dashboard_id: uuid.UUID) -> uuid.UUID:
        """Return the workspace a dashboard belongs to, for access checks."""
        row = await self.pool.fetchrow(
            "SELECT workspace_id FROM dashboards WHERE id = $1",
            dashboard_id,
        )
        if row is None:
            raise ErrNotFound
        return row["workspace_id"]

    async def add_widget(
        self,
        dashboard_id: uuid.UUID,
        w_type: str,
        title: str,
        config: Optional[dict[str, Any]],
        width: int,
    ) -> DashboardWidget:
        """Append a widget to a dashboard."""
        config = config or {}
        raw = json.dumps(config)

        row = await self.pool.fetchrow(
            """
            INSERT INTO dashboard_widgets (dashboard_id, type, title, config, position, width)
            VALUES ($1, $2, $3, $4,
                    COALESCE((SELECT MAX(position)+1 FROM dashboard_widgets WHERE dashboard_id=$1), 0),
                    $5)
            RETURNING id, dashboard_id, type, title, config, position, width
            """,
            dashboard_id,
            w_type,
            title,
            raw,
            width,
        )

        return DashboardWidget(
            id=row["id"],
            dashboard_id=row["dashboard_id"],
            type=row["type"],
            title=row["title"],
            position=row["position"],
            width=row["width"],
            config=_decode_json_object(row["config"]),
        )

    async def delete_widget(self, dashboard_id: uuid.UUID, widget_id: uuid.UUID) -> None:
        """Remove a widget from a dashboard."""
        result = await self.pool.execute(
            "DELETE FROM dashboard_widgets WHERE id = $1 AND dashboard_id = $2",
            widget_id,
            dashboard_id,
        )
        if _rows_affected(result) == 0:
            raise ErrNotFound


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_json_object(raw) -> dict[str, Any]:
    """
    Decode a jsonb `config` column into a dict, defaulting to {} when
    empty/None — mirrors the Go `wdg.Config = map[string]any{}` plus
    `json.Unmarshal` fallback.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw  # asyncpg may already decode jsonb via a codec
    return json.loads(raw)


def _rows_affected(status: str) -> int:
    """Parse asyncpg's `execute` status string, e.g. 'DELETE 1' -> 1."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0