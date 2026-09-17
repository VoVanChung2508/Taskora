import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


USER_COLUMNS = (
    "id, azure_oid, email, display_name, avatar_url, is_system_admin, "
    "is_active, last_login_at, created_at, updated_at"
)


@dataclass
class User:
    id: uuid.UUID
    azure_oid: str
    email: str
    display_name: str
    avatar_url: str
    is_system_admin: bool
    is_active: bool
    last_login_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


def scan_user(row: Optional[asyncpg.Record]) -> User:
    if row is None:
        raise NotFoundError
    return User(
        id=row["id"],
        azure_oid=row["azure_oid"],
        email=row["email"],
        display_name=row["display_name"],
        avatar_url=row["avatar_url"],
        is_system_admin=row["is_system_admin"],
        is_active=row["is_active"],
        last_login_at=row["last_login_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class UserStore:
    """Handles persistence for users."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_from_azure(
        self,
        azure_oid: str,
        email: str,
        display_name: str,
        avatar_url: str,
        is_admin: bool,
    ) -> User:
        """Creates or updates a user based on Azure AD claims. It is called
        on every SSO login so profile fields stay fresh."""
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO users (azure_oid, email, display_name, avatar_url, is_system_admin, last_login_at)
            VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (azure_oid) DO UPDATE
            SET email = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                avatar_url = COALESCE(NULLIF(EXCLUDED.avatar_url, ''), users.avatar_url),
                is_system_admin = users.is_system_admin OR EXCLUDED.is_system_admin,
                last_login_at = now()
            RETURNING {USER_COLUMNS}
            """,
            azure_oid,
            email,
            display_name,
            avatar_url,
            is_admin,
        )
        return scan_user(row)

    async def get_by_id(self, id: uuid.UUID) -> User:
        """Fetches a user by id."""
        row = await self.pool.fetchrow(
            f"SELECT {USER_COLUMNS} FROM users WHERE id = $1", id
        )
        return scan_user(row)

    async def search(self, query: str, limit: int, offset: int) -> list[User]:
        """Returns a page of users, optionally filtered by name or email.

        This replaces an unbounded list_all: an Azure tenant sync leaves
        thousands of rows here, and shipping all of them to the admin page
        froze the browser.
        """
        rows = await self.pool.fetch(
            f"""
            SELECT {USER_COLUMNS} FROM users
            WHERE ($1 = '' OR display_name ILIKE '%'||$1||'%' OR email ILIKE '%'||$1||'%')
            ORDER BY is_system_admin DESC, display_name
            LIMIT $2 OFFSET $3
            """,
            query,
            limit,
            offset,
        )
        return [scan_user(row) for row in rows]

    async def count_users(self, query: str) -> int:
        """Returns how many users match the same filter as search, so the
        UI can show a total and page through it."""
        return await self.pool.fetchval(
            """
            SELECT count(*) FROM users
            WHERE ($1 = '' OR display_name ILIKE '%'||$1||'%' OR email ILIKE '%'||$1||'%')
            """,
            query,
        )

    async def set_system_admin(self, id: uuid.UUID, is_admin: bool) -> None:
        """Sets the is_system_admin flag for a user."""
        await self.pool.execute(
            "UPDATE users SET is_system_admin = $2 WHERE id = $1", id, is_admin
        )