"""Tests for src/engine/event_bus.py — async pub/sub event bus."""
from __future__ import annotations

import asyncio

import pytest

from src.engine.event_bus import Event, EventBus, EventType


# ---------------------------------------------------------------------------
# Subscribe + publish
# ---------------------------------------------------------------------------

class TestSubscribePublish:
    async def test_handler_receives_event(self, event_bus: EventBus):
        received: list[Event] = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe(EventType.PRICE_TICK, handler)

        run_task = asyncio.create_task(event_bus.run())
        await event_bus.publish(Event(type=EventType.PRICE_TICK, data={"price": 100}))
        # Give the dispatch loop time to process
        await asyncio.sleep(0.05)
        await event_bus.stop()
        await run_task

        assert len(received) == 1
        assert received[0].data == {"price": 100}

    async def test_multiple_subscribers_same_event(self, event_bus: EventBus):
        calls_a: list[Event] = []
        calls_b: list[Event] = []

        async def handler_a(event: Event):
            calls_a.append(event)

        async def handler_b(event: Event):
            calls_b.append(event)

        event_bus.subscribe(EventType.SIGNAL_GENERATED, handler_a)
        event_bus.subscribe(EventType.SIGNAL_GENERATED, handler_b)

        run_task = asyncio.create_task(event_bus.run())
        await event_bus.publish(
            Event(type=EventType.SIGNAL_GENERATED, data="sig1")
        )
        await asyncio.sleep(0.05)
        await event_bus.stop()
        await run_task

        assert len(calls_a) == 1
        assert len(calls_b) == 1

    async def test_handler_does_not_receive_other_types(self, event_bus: EventBus):
        received: list[Event] = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe(EventType.PRICE_TICK, handler)

        run_task = asyncio.create_task(event_bus.run())
        await event_bus.publish(
            Event(type=EventType.FUNDING_RATE, data="funding")
        )
        await asyncio.sleep(0.05)
        await event_bus.stop()
        await run_task

        assert len(received) == 0


# ---------------------------------------------------------------------------
# publish_nowait
# ---------------------------------------------------------------------------

class TestPublishNowait:
    async def test_publish_nowait_works(self, event_bus: EventBus):
        received: list[Event] = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe(EventType.ORDER_UPDATE, handler)

        run_task = asyncio.create_task(event_bus.run())
        event_bus.publish_nowait(Event(type=EventType.ORDER_UPDATE, data="ord1"))
        await asyncio.sleep(0.05)
        await event_bus.stop()
        await run_task

        assert len(received) == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    async def test_handler_error_does_not_crash_bus(self, event_bus: EventBus):
        good_calls: list[Event] = []

        async def bad_handler(event: Event):
            raise RuntimeError("boom")

        async def good_handler(event: Event):
            good_calls.append(event)

        event_bus.subscribe(EventType.PRICE_TICK, bad_handler)
        event_bus.subscribe(EventType.PRICE_TICK, good_handler)

        run_task = asyncio.create_task(event_bus.run())
        await event_bus.publish(Event(type=EventType.PRICE_TICK, data="x"))
        await asyncio.sleep(0.1)
        await event_bus.stop()
        await run_task

        # Good handler still called despite bad handler raising
        assert len(good_calls) == 1


# ---------------------------------------------------------------------------
# Lifecycle: run / stop / is_running
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_is_running_property(self, event_bus: EventBus):
        assert event_bus.is_running is False

        run_task = asyncio.create_task(event_bus.run())
        await asyncio.sleep(0.05)
        assert event_bus.is_running is True

        await event_bus.stop()
        await run_task
        assert event_bus.is_running is False

    async def test_stop_sends_shutdown_and_run_terminates(self, event_bus: EventBus):
        run_task = asyncio.create_task(event_bus.run())
        await asyncio.sleep(0.05)
        await event_bus.stop()

        # run() should complete within a reasonable time
        await asyncio.wait_for(run_task, timeout=3.0)
        assert event_bus.is_running is False
