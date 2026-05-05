"""Cross-service message bus using Redis pub/sub.

All services communicate through Redis channels:
  signals:screener     — screener → paper trading, engine
  signals:strategy     — engine strategies → paper trading
  commands:engine      — telegram bot → engine
  notifications:telegram — all services → telegram bot
  events:trades        — engine → paper trading, telegram
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import structlog

logger = structlog.get_logger()

# Channel names
CH_SIGNALS_SCREENER = "signals:screener"
CH_SIGNALS_STRATEGY = "signals:strategy"
CH_COMMANDS_ENGINE = "commands:engine"
CH_NOTIFICATIONS_TG = "notifications:telegram"
CH_EVENTS_TRADES = "events:trades"

# Handler type
MessageHandler = Callable[[dict], Coroutine[Any, Any, None]]


class RedisBus:
    """Async Redis pub/sub wrapper for inter-service communication."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis_url = redis_url
        self._redis = None
        self._pubsub = None
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._running = False

    async def connect(self) -> None:
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        await self._redis.ping()
        logger.info("redis_bus_connected", url=self._redis_url)

    async def disconnect(self) -> None:
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()
        logger.info("redis_bus_disconnected")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, channel: str, payload: dict, source: str = "") -> None:
        """Publish a JSON message to a channel."""
        message = {
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        await self._redis.publish(channel, json.dumps(message, default=str))

    async def notify(self, text: str, source: str = "") -> None:
        """Shortcut: publish a Telegram notification."""
        await self.publish(CH_NOTIFICATIONS_TG, {"text": text}, source=source)

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    def on(self, channel: str, handler: MessageHandler) -> None:
        """Register a handler for a channel. Call before run()."""
        self._handlers.setdefault(channel, []).append(handler)

    async def run(self) -> None:
        """Subscribe to all registered channels and dispatch messages."""
        if not self._handlers:
            logger.warning("redis_bus_no_handlers")
            return

        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(*self._handlers.keys())
        self._running = True
        logger.info("redis_bus_listening", channels=list(self._handlers.keys()))

        while self._running:
            try:
                msg = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if msg is None:
                    continue
                if msg["type"] != "message":
                    continue

                channel = msg["channel"]
                try:
                    data = json.loads(msg["data"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("redis_bus_bad_message", channel=channel)
                    continue

                handlers = self._handlers.get(channel, [])
                for handler in handlers:
                    try:
                        await handler(data)
                    except Exception:
                        logger.exception("redis_bus_handler_error",
                                         channel=channel,
                                         handler=handler.__qualname__)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("redis_bus_recv_error")
                await asyncio.sleep(1)

        logger.info("redis_bus_stopped")

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[str]:
        """Simple key-value get."""
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ex: int = 0) -> None:
        """Simple key-value set with optional expiry in seconds."""
        if ex:
            await self._redis.set(key, value, ex=ex)
        else:
            await self._redis.set(key, value)

    async def keys(self, pattern: str) -> list[str]:
        """Return keys matching *pattern*."""
        return await self._redis.keys(pattern)

    async def ttl(self, key: str) -> int:
        """Return TTL of a key in seconds (-1 = no expiry, -2 = missing)."""
        return await self._redis.ttl(key)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def start_heartbeat(self, service_name: str, interval: int = 30) -> None:
        """Spawn a background task that sets heartbeat:<service_name> every *interval* seconds.

        The key has a TTL of 3× interval so it expires if the service stops.
        """
        ttl = interval * 3

        async def _beat() -> None:
            while self._running or not self._pubsub:  # also run before run()
                try:
                    ts = datetime.now(timezone.utc).isoformat()
                    await self._redis.set(f"heartbeat:{service_name}", ts, ex=ttl)
                except Exception:
                    logger.warning("heartbeat_write_failed", service=service_name)
                await asyncio.sleep(interval)

        self._running = True  # ensure loop runs even without pubsub
        asyncio.get_event_loop().create_task(_beat())
