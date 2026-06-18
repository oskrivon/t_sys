"""Bybit collection feed — drives parse → BookState → Parquet recorder.

Message handling (`handle_message`) is deliberately split from the socket loop
(`run`) so the full parse→book→record path is unit-testable by injecting scripted
frames and a deterministic clock — no network required. The socket loop reuses
the same receive/reconnect shape as ``src/core/websocket/bybit_ws.py``.

Storage is lossless: every raw book diff and trade is written, so books can be
reconstructed offline at any cadence. An in-memory ``BookState`` is kept per
symbol for periodic ``book_snap`` seeds (cheap replay restart points) and to
detect when a re-seed from REST is needed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

import structlog

from src.icebreaker.book import BookState
from src.icebreaker.parse_bybit import parse_orderbook, parse_trades
from src.icebreaker.recorder import ParquetRecorder

logger = structlog.get_logger()

EXCHANGE = "bybit"


def date_from_ns(recv_ts_ns: int) -> str:
    """UTC date partition string for a receive timestamp (ns)."""
    return datetime.fromtimestamp(recv_ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d")


class BybitCollectorFeed:
    def __init__(
        self,
        recorder: ParquetRecorder,
        *,
        snap_every_n_deltas: int = 200,
        clock: Callable[[], int] = time.time_ns,
        date_fn: Callable[[int], str] = date_from_ns,
        on_resync: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.recorder = recorder
        self.snap_every_n_deltas = snap_every_n_deltas
        self.clock = clock
        self.date_fn = date_fn
        self.on_resync = on_resync
        self.books: dict[str, BookState] = {}
        self._deltas_since_snap: dict[str, int] = {}
        self.stats = {"book_msgs": 0, "trade_msgs": 0, "resyncs": 0, "ignored": 0}

    # ------------------------------------------------------------------
    # Message handling (sync, pure of I/O — fully unit-testable)
    # ------------------------------------------------------------------
    def handle_message(self, msg: dict, recv_ts_ns: Optional[int] = None) -> None:
        if recv_ts_ns is None:
            recv_ts_ns = self.clock()
        topic = msg.get("topic", "")
        if topic.startswith("orderbook."):
            self._handle_orderbook(msg, recv_ts_ns)
        elif topic.startswith("publicTrade."):
            self._handle_trades(msg, recv_ts_ns)
        else:
            self.stats["ignored"] += 1

    def _handle_orderbook(self, msg: dict, recv_ts_ns: int) -> None:
        ob = parse_orderbook(msg)
        if ob is None:
            self.stats["ignored"] += 1
            return
        self.stats["book_msgs"] += 1
        date = self.date_fn(recv_ts_ns)

        # 1) Lossless raw record of every changed level.
        for side, levels in (("bid", ob.bids), ("ask", ob.asks)):
            for price, qty in levels:
                self.recorder.record(EXCHANGE, ob.symbol, date, "book_diff", {
                    "recv_ts_ns": recv_ts_ns,
                    "exch_ts_ms": ob.exch_ts_ms,
                    "update_id": ob.update_id if ob.update_id is not None else -1,
                    "kind": ob.kind,
                    "side": side,
                    "price": price,
                    "qty": qty,
                })

        # 2) Maintain a live book for snapshots + resync detection.
        book = self.books.setdefault(ob.symbol, BookState())
        if ob.kind == "snapshot":
            book.apply_snapshot(ob.bids, ob.asks, ob.update_id)
            self._deltas_since_snap[ob.symbol] = 0
            self._write_snapshot(ob.symbol, book, recv_ts_ns, ob.exch_ts_ms)
        else:
            applied = book.apply_delta(ob.bids, ob.asks, ob.update_id)
            if not applied and book.resync_needed:
                self.stats["resyncs"] += 1
                logger.warning("icebreaker_resync_needed", symbol=ob.symbol)
                if self.on_resync:
                    self.on_resync(ob.symbol)
                return
            n = self._deltas_since_snap.get(ob.symbol, 0) + 1
            self._deltas_since_snap[ob.symbol] = n
            if n >= self.snap_every_n_deltas:
                self._write_snapshot(ob.symbol, book, recv_ts_ns, ob.exch_ts_ms)
                self._deltas_since_snap[ob.symbol] = 0

    def _write_snapshot(self, symbol: str, book: BookState,
                        recv_ts_ns: int, exch_ts_ms: int) -> None:
        date = self.date_fn(recv_ts_ns)
        for side, levels in (("bid", book.bids.items()), ("ask", book.asks.items())):
            for price, qty in levels:
                self.recorder.record(EXCHANGE, symbol, date, "book_snap", {
                    "recv_ts_ns": recv_ts_ns,
                    "exch_ts_ms": exch_ts_ms,
                    "side": side,
                    "price": price,
                    "qty": qty,
                })

    def _handle_trades(self, msg: dict, recv_ts_ns: int) -> None:
        rows = parse_trades(msg)
        if not rows:
            self.stats["ignored"] += 1
            return
        self.stats["trade_msgs"] += 1
        date = self.date_fn(recv_ts_ns)
        for t in rows:
            self.recorder.record(EXCHANGE, t.symbol, date, "trades", {
                "recv_ts_ns": recv_ts_ns,
                "exch_ts_ms": t.exch_ts_ms,
                "price": t.price,
                "qty": t.qty,
                "side": t.side,
            })

    # ------------------------------------------------------------------
    # Socket loop (thin; validated by a live smoke run)
    # ------------------------------------------------------------------
    async def run(self, ws, *, stop_after: Optional[int] = None) -> None:
        """Receive loop. ``ws`` must expose an awaitable ``recv()`` returning JSON
        text. ``stop_after`` (messages) bounds the loop for smoke tests."""
        import json
        seen = 0
        while True:
            raw = await ws.recv()
            recv_ts_ns = self.clock()
            seen += 1  # count every received frame toward stop_after, even bad ones
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                msg = None
            if msg is not None:
                self.handle_message(msg, recv_ts_ns)
            if stop_after is not None and seen >= stop_after:
                return
