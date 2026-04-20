"""In-process async event bus. Zero serialization overhead."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger()


class EventType(str, Enum):
    PRICE_TICK = "price_tick"
    FUNDING_RATE = "funding_rate"
    ORDER_UPDATE = "order_update"
    SIGNAL_GENERATED = "signal_generated"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    ENGINE_SHUTDOWN = "engine_shutdown"


@dataclass
class Event:
    type: EventType
    data: Any
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = ""


# Type alias for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Lightweight async pub/sub. Handlers run as tasks — slow handler won't block others."""

    def __init__(self) -> None:
        self._subscribers: dict[EventType, list[EventHandler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        self._dispatch_tasks: set[asyncio.Task] = set()

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    def publish_nowait(self, event: Event) -> None:
        self._queue.put_nowait(event)

    async def run(self) -> None:
        """Main dispatch loop. Runs until ENGINE_SHUTDOWN event."""
        self._running = True
        logger.info("event_bus_started")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if event.type == EventType.ENGINE_SHUTDOWN:
                self._running = False
                break

            handlers = self._subscribers.get(event.type, [])
            for handler in handlers:
                task = asyncio.create_task(self._safe_call(handler, event))
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)

        # Wait for in-flight handlers (max 5s)
        if self._dispatch_tasks:
            await asyncio.wait(self._dispatch_tasks, timeout=5.0)
        logger.info("event_bus_stopped")

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("event_handler_error",
                             event_type=event.type.value,
                             source=event.source,
                             handler=handler.__qualname__)

    async def stop(self) -> None:
        await self.publish(Event(type=EventType.ENGINE_SHUTDOWN, data=None, source="bus"))

    @property
    def is_running(self) -> bool:
        return self._running
