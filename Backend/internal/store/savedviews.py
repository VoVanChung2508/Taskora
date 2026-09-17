import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a requested row does not exist or is not accessible."""
    pass


@dataclass
class SavedView:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_id: Optional[uuid.UUID]
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = None
    shared: bool = False


class SavedViewStore:
    """Persists per-project saved board views."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_for_user(
        self, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[SavedView]:
        """Returns a project's views visible to a user: their own plus the shared ones."""
        rows = await self.pool.fetch(
            """
            SELECT id, project_id, owner_id, name, config, created_at
            FROM saved_views
            WHERE project_id = $1 AND (owner_id IS NULL OR owner_id = $2)
            ORDER BY created_at
            """,
            project_id,
            user_id,
        )

        out: list[SavedView] = []
        for row in rows:
            cfg = row["config"]
            config = json.loads(cfg) if cfg else {}
            view = SavedView(
                id=row["id"],
                project_id=row["project_id"],
                owner_id=row["owner_id"],
                name=row["name"],
                config=config,
                created_at=row["created_at"],
                shared=row["owner_id"] is None,
            )
            out.append(view)
        return out

    async def create(
        self,
        project_id: uuid.UUID,
        owner: Optional[uuid.UUID],
        name: str,
        config: Optional[dict[str, Any]] = None,
    ) -> SavedView:
        """Stores a view. Pass owner=None to share it with the project."""
        if config is None:
            config = {}
        raw = json.dumps(config)

        row = await self.pool.fetchrow(
            """
            INSERT INTO saved_views (project_id, owner_id, name, config)
            VALUES ($1, $2, $3, $4)
            RETURNING id, project_id, owner_id, name, config, created_at
            """,
            project_id,
            owner,
            name,
            raw,
        )

        cfg = row["config"]
        parsed_config = json.loads(cfg) if cfg else {}
        return SavedView(
            id=row["id"],
            project_id=row["project_id"],
            owner_id=row["owner_id"],
            name=row["name"],
            config=parsed_config,
            created_at=row["created_at"],
            shared=row["owner_id"] is None,
        )

    async def delete(
        self,
        project_id: uuid.UUID,
        view_id: uuid.UUID,
        user_id: uuid.UUID,
        allow_shared: bool = False,
    ) -> None:
        """Removes a view. A user may only delete their own view or, when
        allow_shared is set (owner/admin), a shared one."""
        if allow_shared:
            query = """
                DELETE FROM saved_views
                WHERE id=$1 AND project_id=$2 AND (owner_id=$3 OR owner_id IS NULL)
            """
        else:
            query = """
                DELETE FROM saved_views
                WHERE id=$1 AND project_id=$2 AND owner_id=$3
            """

        result = await self.pool.execute(query, view_id, project_id, user_id)
        # asyncpg trả về chuỗi kiểu "DELETE <n>"
        if result.split()[-1] == "0":
            raise NotFoundError