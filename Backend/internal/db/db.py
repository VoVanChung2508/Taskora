"""PostgreSQL connection pool and migration runner.

Python port of the Go `db` package. Requires:
    pip install asyncpg

Uses `asyncio` + `asyncpg`, the closest Python equivalent to pgx's
context-based async pool. Requires Python 3.11+ for `asyncio.timeout`
(swap for `asyncio.wait_for(...)` on older versions).

Migration files are expected in a `migrations/` directory next to this
module — the analogue of Go's `//go:embed migrations/*.sql`. If you package
this module for distribution, make sure `migrations/*.sql` is included as
package data (e.g. `package_data`/`MANIFEST.in` for setuptools, or the
equivalent for your build tool) so it ships the same way an embedded FS
would.
"""

from __future__ import annotations

import asyncio
import importlib.resources as resources
from typing import List

import asyncpg

_MIGRATIONS_PACKAGE = __package__ or __name__
_MIGRATIONS_DIR = "migrations"

_POOL_MAX_SIZE = 10
# asyncpg has no exact match for pgx's MaxConnLifetime; the closest
# analogue is max_inactive_connection_lifetime (seconds a connection may
# sit idle in the pool before being recycled).
_POOL_MAX_CONN_LIFETIME = 3600  # 1 hour
_PING_TIMEOUT = 5  # seconds


async def connect(dsn: str) -> asyncpg.Pool:
    """Open an asyncpg connection pool and verify connectivity."""
    try:
        pool = await asyncpg.create_pool(
            dsn,
            max_size=_POOL_MAX_SIZE,
            max_inactive_connection_lifetime=_POOL_MAX_CONN_LIFETIME,
        )
    except Exception as exc:
        raise RuntimeError(f"create pool: {exc}") from exc

    try:
        async with asyncio.timeout(_PING_TIMEOUT):
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
    except Exception as exc:
        await pool.close()
        raise RuntimeError(f"ping database: {exc}") from exc

    return pool


async def migrate(pool: asyncpg.Pool) -> None:
    """Apply any pending .sql migrations in lexical order.

    Tracks applied migrations in a `schema_migrations` table and runs each
    new file in its own transaction.
    """
    try:
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    except Exception as exc:
        raise RuntimeError(f"ensure schema_migrations: {exc}") from exc

    try:
        names = _list_migration_files()
    except Exception as exc:
        raise RuntimeError(f"read migrations dir: {exc}") from exc

    for name in names:
        try:
            exists = await pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = $1)",
                name,
            )
        except Exception as exc:
            raise RuntimeError(f"check migration {name}: {exc}") from exc
        if exists:
            continue

        try:
            sql_text = _read_migration_file(name)
        except Exception as exc:
            raise RuntimeError(f"read migration {name}: {exc}") from exc

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(sql_text)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)",
                        name,
                    )
        except Exception as exc:
            raise RuntimeError(f"apply migration {name}: {exc}") from exc


def _list_migration_files() -> List[str]:
    """Return migration file names, sorted lexically, directories excluded."""
    migrations_dir = resources.files(_MIGRATIONS_PACKAGE).joinpath(_MIGRATIONS_DIR)
    names = [entry.name for entry in migrations_dir.iterdir() if entry.is_file()]
    return sorted(names)


def _read_migration_file(name: str) -> str:
    migrations_dir = resources.files(_MIGRATIONS_PACKAGE).joinpath(_MIGRATIONS_DIR)
    return migrations_dir.joinpath(name).read_text(encoding="utf-8")