"""In-process realtime fan-out for project SSE streams."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


_QUEUE_MAXSIZE = 16


@dataclass
class Event:
    type: str
    project_id: uuid.UUID
    actor_id: Optional[uuid.UUID] = None
    payload: Optional[dict[str, Any]] = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "projectId": str(self.project_id),
        }
        if self.actor_id is not None:
            data["actorId"] = str(self.actor_id)
        if self.payload:
            data["payload"] = self.payload
        return data


@dataclass(eq=False)
class _Subscriber:
    project_id: uuid.UUID
    queue: asyncio.Queue[Event] = field(
        default_factory=lambda: asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
    )


class Hub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[_Subscriber, None] = {}

    async def subscribe(
        self,
        project_id: uuid.UUID,
    ) -> tuple[
        asyncio.Queue[Event],
        Callable[[], Awaitable[None]],
    ]:
        subscriber = _Subscriber(project_id=project_id)

        async with self._lock:
            self._subs[subscriber] = None

        async def unsubscribe() -> None:
            async with self._lock:
                self._subs.pop(subscriber, None)

        return subscriber.queue, unsubscribe

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subscribers = tuple(self._subs)

        for subscriber in subscribers:
            if subscriber.project_id != event.project_id:
                continue

            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Realtime events are refresh hints. Never stall a mutation
                # because one browser tab stopped consuming its SSE queue.
                pass

    async def count(self) -> int:
        async with self._lock:
            return len(self._subs)
