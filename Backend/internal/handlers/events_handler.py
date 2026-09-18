from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import HTTPException
from starlette.responses import StreamingResponse

from ..auth.session import get_user_id
from ..realtime.hub import Event

if TYPE_CHECKING:
    from .handlers import Handlers


SSE_HEARTBEAT = 25.0


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )
    return user_id


async def project_events(
    self: "Handlers",
    project_id: uuid.UUID,
):
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    queue, unsubscribe = await self.hub.subscribe(project.id)

    async def stream() -> AsyncIterator[bytes]:
        try:
            ready = json.dumps(
                {"projectId": str(project.id)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"event: ready\ndata: {ready}\n\n".encode("utf-8")

            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=SSE_HEARTBEAT,
                    )
                except asyncio.TimeoutError:
                    # SSE comment frame: EventSource ignores it, proxies see
                    # traffic and keep the connection open.
                    yield b": ping\n\n"
                    continue

                try:
                    payload = json.dumps(
                        event.to_json(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                except (TypeError, ValueError):
                    continue

                yield (
                    f"event: {event.type}\n"
                    f"data: {payload}\n\n"
                ).encode("utf-8")
        except asyncio.CancelledError:
            raise
        finally:
            await unsubscribe()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def publish(
    self: "Handlers",
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if self.hub is None:
        return

    await self.hub.publish(
        Event(
            type=event_type,
            project_id=project_id,
            actor_id=actor_id,
            payload=payload,
        )
    )
