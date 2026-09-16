"""Broadcasts change events to connected clients.

Transport is Server-Sent Events rather than WebSocket: the traffic here is
one-directional (server -> client; clients still mutate through the REST
API), SSE needs no extra dependency, reconnects automatically in the
browser, and survives proxies that mishandle WebSocket upgrades.

Python port of the Go `realtime` package. Uses `asyncio.Queue` per
subscriber (the closest equivalent of a Go buffered channel) and
`asyncio.Lock` instead of `sync.RWMutex`; assumes a single-event-loop
(asyncio) server such as FastAPI/Starlette/aiohttp.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

_QUEUE_MAXSIZE = 16


@dataclass
class Event:
    """A single change notification sent to subscribers."""

    # type is the event name, e.g. "task.updated" or "chat.message".
    type: str
    # project_id scopes the event; subscribers only receive their project.
    project_id: uuid.UUID
    # actor_id is who caused it, so clients can skip echoing their own action.
    actor_id: Optional[uuid.UUID] = None
    # payload carries event-specific fields (ids, changed values...).
    payload: Optional[Dict[str, Any]] = None

    def to_json(self) -> Dict[str, Any]:
        """JSON-serializable dict, mirroring the Go `json` tags:
        `actor_id`/`payload` are dropped when unset, matching `omitempty`.
        """
        d: Dict[str, Any] = {"type": self.type, "projectId": str(self.project_id)}
        if self.actor_id is not None:
            d["actorId"] = str(self.actor_id)
        if self.payload:
            d["payload"] = self.payload
        return d


@dataclass(eq=False)  # identity-based equality/hash, like a Go *subscriber
class _Subscriber:
    """One connected client stream."""

    project_id: uuid.UUID
    queue: "asyncio.Queue[Event]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    )


class Hub:
    """Fans events out to subscribers grouped by project."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: Dict[_Subscriber, None] = {}

    async def subscribe(
        self, project_id: uuid.UUID
    ) -> Tuple["asyncio.Queue[Event]", Callable[[], Awaitable[None]]]:
        """Register a listener for one project.

        Returns the subscriber's queue (bounded, maxsize 16) and an async
        `unsubscribe()` callable that must be awaited to release it, e.g.:

            queue, unsubscribe = await hub.subscribe(project_id)
            try:
                while True:
                    event = await queue.get()
                    yield event.to_json()
            finally:
                await unsubscribe()
        """
        s = _Subscriber(project_id=project_id)
        async with self._lock:
            self._subs[s] = None

        async def unsubscribe() -> None:
            async with self._lock:
                self._subs.pop(s, None)

        return s.queue, unsubscribe

    async def publish(self, event: Event) -> None:
        """Deliver an event to every subscriber of its project. Slow
        consumers are skipped rather than blocking the caller — dropping a
        refresh hint is preferable to stalling a request handler.
        """
        async with self._lock:
            subs = list(self._subs)

        for s in subs:
            if s.project_id != event.project_id:
                continue
            try:
                s.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop rather than block the publisher

    async def count(self) -> int:
        """Report how many streams are connected (used by tests/diagnostics)."""
        async with self._lock:
            return len(self._subs)