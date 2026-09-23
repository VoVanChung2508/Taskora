from contextlib import asynccontextmanager

from internal.db.db import connect
from internal.config.config import load
from internal.server.router import new_app
from internal.handlers.handlers import new
from internal.auth.session import SessionManager
from internal.store.store import Store


class LazyPool:
    """Holds the database pool until the ASGI server finishes starting."""

    def __init__(self) -> None:
        self._pool = None

    def set(self, database_pool) -> None:
        self._pool = database_pool

    def __getattr__(self, name):
        if self._pool is None:
            raise RuntimeError("database pool is not ready")
        return getattr(self._pool, name)


cfg = load()
pool = LazyPool()
store = Store(pool)
sessions = SessionManager(
    cfg.session_secret,
    cfg.session_ttl,
    secure=cfg.env != "development",
)
handlers = new(cfg, store, sessions, None, None)
app = new_app(cfg, handlers, sessions)


@asynccontextmanager
async def lifespan(_app):
    database_pool = await connect(cfg.database_url)
    pool.set(database_pool)
    try:
        yield
    finally:
        await database_pool.close()


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="172.20.10.2",
        port=int(cfg.port),
        reload=cfg.env == "development",
    )
