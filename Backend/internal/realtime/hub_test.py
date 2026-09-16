"""Tests for `realtime.py`.

Python port of the Go `realtime` package tests. Run with:
    pip install pytest pytest-asyncio
    pytest test_realtime.py

Two adaptations from the Go tests, both noted inline:
  - Go channels can be closed; `asyncio.Queue` cannot. "Unsubscribe stops
    delivery" is verified by asserting no event arrives, instead of
    asserting the channel reports closed.
  - `TestPublishOnNilHubIsSafe` relies on Go's nil-pointer-receiver
    semantics (`h.Publish` on a nil `*Hub` is safe because of an explicit
    `if h == nil` guard). Python has no equivalent — calling a method on
    `None` always raises — so that test has no direct port. The closest
    meaningful check, `test_publish_with_no_subscribers_is_safe`, confirms
    publishing to a real hub with nothing listening doesn't raise.
"""

import asyncio
import uuid

import pytest

from realtime import Event, Hub


async def _recv(queue: "asyncio.Queue[Event]", timeout: float = 1.0) -> Event:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        pytest.fail("timed out waiting for event")


@pytest.mark.asyncio
async def test_publish_reaches_project_subscriber():
    h = Hub()
    project = uuid.uuid4()

    queue, unsub = await h.subscribe(project)
    try:
        await h.publish(Event(type="task.created", project_id=project))
        got = await _recv(queue)
        assert got.type == "task.created", f"type = {got.type!r}, want task.created"
    finally:
        await unsub()


@pytest.mark.asyncio
async def test_publish_is_scoped_to_project():
    h = Hub()
    mine, other = uuid.uuid4(), uuid.uuid4()

    queue, unsub = await h.subscribe(mine)
    try:
        # An event for a different project must not leak into this stream.
        await h.publish(Event(type="task.created", project_id=other))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.1)
    finally:
        await unsub()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    h = Hub()
    project = uuid.uuid4()

    queue, unsub = await h.subscribe(project)
    assert await h.count() == 1, f"count() = {await h.count()}, want 1"

    await unsub()
    assert await h.count() == 0, f"count() after unsubscribe = {await h.count()}, want 0"

    # Publishing afterwards must not raise, and must not reach the queue.
    await h.publish(Event(type="task.created", project_id=project))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_unsubscribe_is_idempotent():
    h = Hub()
    _, unsub = await h.subscribe(uuid.uuid4())
    await unsub()
    await unsub()  # must not raise or double-remove


@pytest.mark.asyncio
async def test_publish_skips_slow_subscriber():
    h = Hub()
    project = uuid.uuid4()

    _, unsub = await h.subscribe(project)
    try:
        # Overflow the 16-slot buffer; publish must drop rather than block.
        async def flood():
            for _ in range(100):
                await h.publish(Event(type="task.updated", project_id=project))

        await asyncio.wait_for(flood(), timeout=2.0)
    finally:
        await unsub()


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_safe():
    h = Hub()
    await h.publish(Event(type="task.created", project_id=uuid.uuid4()))