"""Live Bybit public WS client for the icebreaker collector.

Connects to the Bybit v5 linear public stream, subscribes to ``orderbook.<depth>``
and ``publicTrade`` for the watchlist symbols, and drives a ``BybitCollectorFeed``.
Reuses the same connect / ping / reconnect shape as ``core/websocket/bybit_ws.py``.

On a detected sequence gap the feed flags ``resync_needed``; we unsubscribe +
resubscribe that symbol's orderbook topic, which makes Bybit resend a fresh
snapshot (no REST round-trip needed).

The pure bits — topic building and subscribe-message batching — are unit tested;
the socket loop is validated by a live smoke run.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Iterable, Iterator, Optional

import structlog
import websockets

from src.icebreaker.collector import BybitCollectorFeed

logger = structlog.get_logger()

BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
MAX_ARGS_PER_REQUEST = 10  # Bybit caps topics per subscribe frame


def unified_to_raw(symbol: str) -> str:
    """'BAN/USDT:USDT' -> 'BANUSDT' (Bybit WS topic symbol format)."""
    return symbol.replace("/", "").replace(":USDT", "")


def build_topics(symbols: Iterable[str], depth: int) -> list[str]:
    """Order-book + public-trade topics for each unified symbol."""
    topics: list[str] = []
    for s in symbols:
        raw = unified_to_raw(s)
        topics.append(f"orderbook.{depth}.{raw}")
        topics.append(f"publicTrade.{raw}")
    return topics


def batch_subscribe_frames(topics: list[str],
                           batch: int = MAX_ARGS_PER_REQUEST) -> Iterator[str]:
    """Yield subscribe-op JSON frames, ≤ ``batch`` topics each."""
    for i in range(0, len(topics), batch):
        yield json.dumps({"op": "subscribe", "args": topics[i:i + batch]})


class BybitCollectorClient:
    def __init__(
        self,
        feed: BybitCollectorFeed,
        symbols: Iterable[str],
        *,
        depth: int = 50,
        flush_interval_s: float = 5.0,
        url: str = BYBIT_WS_PUBLIC,
    ) -> None:
        self.feed = feed
        self.symbols = list(symbols)
        self.depth = depth
        self.flush_interval_s = flush_interval_s
        self.url = url
        self._running = False
        self._resync: set[str] = set()
        # feed flags a raw symbol when it needs a fresh snapshot
        self.feed.on_resync = self._resync.add
        self.stats = {"reconnects": 0, "resubscribes": 0, "frames": 0}

    async def run(self, duration_s: Optional[float] = None) -> None:
        """Collect until ``duration_s`` elapses (None = forever / until stop())."""
        self._running = True
        deadline = time.monotonic() + duration_s if duration_s else None
        backoff = 1.0
        try:
            while self._running:
                try:
                    await self._session(deadline)
                except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                    if not self._running:
                        break
                    self.stats["reconnects"] += 1
                    logger.warning("icebreaker_ws_reconnect", error=str(e), backoff=backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    break
        finally:
            self.feed.recorder.flush_all()

    def stop(self) -> None:
        self._running = False

    async def _session(self, deadline: Optional[float]) -> None:
        async with websockets.connect(
            self.url, ping_interval=20, ping_timeout=10, close_timeout=5,
        ) as ws:
            for frame in batch_subscribe_frames(build_topics(self.symbols, self.depth)):
                await ws.send(frame)
            logger.info("icebreaker_ws_subscribed", symbols=len(self.symbols))
            last_flush = time.monotonic()
            while self._running:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._running = False
                    return
                timeout = 5.0
                if deadline is not None:
                    timeout = min(timeout, max(0.1, deadline - now))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    await ws.send(json.dumps({"op": "ping"}))
                    raw = None
                if raw is not None:
                    recv_ts_ns = time.time_ns()
                    self.stats["frames"] += 1
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        msg = None
                    if msg is not None:
                        self.feed.handle_message(msg, recv_ts_ns)

                if self._resync:
                    await self._do_resync(ws)

                if time.monotonic() - last_flush >= self.flush_interval_s:
                    self.feed.recorder.flush_all()
                    last_flush = time.monotonic()

    async def _do_resync(self, ws) -> None:
        raws = list(self._resync)
        self._resync.clear()
        topics = [f"orderbook.{self.depth}.{r}" for r in raws]
        await ws.send(json.dumps({"op": "unsubscribe", "args": topics}))
        for frame in batch_subscribe_frames(topics):
            await ws.send(frame)
        self.stats["resubscribes"] += len(raws)
        logger.info("icebreaker_resubscribe", symbols=raws)
