"""Unit tests for the Bybit collection feed (Этап 1) — no network."""
from __future__ import annotations

import json
from itertools import count

import pyarrow.parquet as pq
import pytest

from src.icebreaker.collector import BybitCollectorFeed, date_from_ns
from src.icebreaker.recorder import ParquetRecorder


FIXED_DATE = "2026-06-18"


def _feed(tmp_path, **kw):
    rec = ParquetRecorder(tmp_path, flush_rows=kw.pop("flush_rows", 1000))
    # deterministic clock + fixed partition date
    clk = count(1)
    feed = BybitCollectorFeed(
        rec, clock=lambda: next(clk), date_fn=lambda ns: FIXED_DATE, **kw)
    return feed, rec


def _ob(kind, u, bids, asks, sym="BANUSDT"):
    return {"topic": f"orderbook.50.{sym}", "type": kind, "ts": u,
            "data": {"s": sym, "b": bids, "a": asks, "u": u}}


def _read(tmp_path, kind, sym="BANUSDT"):
    base = tmp_path / "exchange=bybit" / f"symbol={sym}" / f"date={FIXED_DATE}" / kind
    files = sorted(base.glob("part-*.parquet"))
    if not files:
        return None
    return pq.read_table(files)


# ---------------------------------------------------------------------------
# Orderbook path
# ---------------------------------------------------------------------------

def test_snapshot_records_book_diff_and_snap(tmp_path):
    feed, rec = _feed(tmp_path)
    feed.handle_message(_ob("snapshot", 100, [["0.10", "1000"]], [["0.11", "500"]]))
    rec.flush_all()

    diffs = _read(tmp_path, "book_diff")
    assert diffs.num_rows == 2                      # one bid + one ask level
    assert set(diffs.column("kind").to_pylist()) == {"snapshot"}

    snaps = _read(tmp_path, "book_snap")
    assert snaps.num_rows == 2                      # snapshot also seeds a book_snap


def test_delta_applies_and_records(tmp_path):
    feed, rec = _feed(tmp_path)
    feed.handle_message(_ob("snapshot", 100, [["0.10", "1000"]], [["0.11", "500"]]))
    feed.handle_message(_ob("delta", 101, [["0.10", "0"]], []))  # remove bid
    rec.flush_all()

    diffs = _read(tmp_path, "book_diff")
    # 2 from snapshot + 1 from delta
    assert diffs.num_rows == 3
    # in-memory book reflects the removal
    assert feed.books["BANUSDT"].best_bid is None
    assert feed.stats["book_msgs"] == 2


def test_delta_before_snapshot_triggers_resync(tmp_path):
    seen = []
    feed, rec = _feed(tmp_path, on_resync=seen.append)
    feed.handle_message(_ob("delta", 5, [["0.10", "1"]], []))
    assert seen == ["BANUSDT"]
    assert feed.stats["resyncs"] == 1
    assert feed.books["BANUSDT"].resync_needed is True


def test_periodic_snapshot_cadence(tmp_path):
    feed, rec = _feed(tmp_path, snap_every_n_deltas=3)
    feed.handle_message(_ob("snapshot", 1, [["0.10", "1000"]], [["0.11", "500"]]))
    # 3 deltas -> one extra book_snap emitted at the 3rd
    for u in (2, 3, 4):
        feed.handle_message(_ob("delta", u, [["0.10", str(1000 + u)]], []))
    rec.flush_all()
    snaps = _read(tmp_path, "book_snap")
    # snapshot seed (2 rows) + periodic snap at 3rd delta (2 rows) = 4
    assert snaps.num_rows == 4


# ---------------------------------------------------------------------------
# Trade path
# ---------------------------------------------------------------------------

def test_trades_recorded(tmp_path):
    feed, rec = _feed(tmp_path)
    feed.handle_message({
        "topic": "publicTrade.BANUSDT", "type": "snapshot", "ts": 1,
        "data": [{"T": 1672304486865, "s": "BANUSDT", "S": "Sell", "v": "100", "p": "0.105"}],
    })
    rec.flush_all()
    trades = _read(tmp_path, "trades")
    assert trades.num_rows == 1
    assert trades.column("side").to_pylist() == ["sell"]
    assert feed.stats["trade_msgs"] == 1


def test_unknown_topic_ignored(tmp_path):
    feed, rec = _feed(tmp_path)
    feed.handle_message({"topic": "tickers.BTCUSDT", "data": {}})
    assert feed.stats["ignored"] == 1
    assert rec.buffered() == 0


def test_recv_ts_is_single_clock(tmp_path):
    """Every row stamped by the collector clock, not the exchange ts."""
    feed, rec = _feed(tmp_path)
    feed.handle_message(_ob("snapshot", 100, [["0.10", "1000"]], []))
    rec.flush_all()
    diffs = _read(tmp_path, "book_diff")
    recv = diffs.column("recv_ts_ns").to_pylist()
    assert all(isinstance(x, int) for x in recv)
    # exch ts (from msg) differs from recv ts (from injected clock)
    assert diffs.column("exch_ts_ms").to_pylist() == [100]
    assert recv == [1]


# ---------------------------------------------------------------------------
# run() loop with a fake socket
# ---------------------------------------------------------------------------

class _FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)
    async def recv(self):
        if not self._frames:
            raise AssertionError("recv past end")
        return self._frames.pop(0)


async def test_run_loop_consumes_frames_until_stop(tmp_path):
    feed, rec = _feed(tmp_path)
    frames = [
        json.dumps(_ob("snapshot", 100, [["0.10", "1000"]], [["0.11", "500"]])),
        json.dumps({"topic": "publicTrade.BANUSDT", "type": "snapshot", "ts": 1,
                    "data": [{"T": 1, "s": "BANUSDT", "S": "Buy", "v": "5", "p": "0.1"}]}),
    ]
    ws = _FakeWS(frames)
    await feed.run(ws, stop_after=2)
    rec.flush_all()
    assert feed.stats["book_msgs"] == 1
    assert feed.stats["trade_msgs"] == 1
    assert _read(tmp_path, "trades").num_rows == 1


async def test_run_loop_skips_bad_json(tmp_path):
    feed, rec = _feed(tmp_path)
    ws = _FakeWS(["not json", json.dumps(_ob("snapshot", 1, [["0.1", "1"]], []))])
    await feed.run(ws, stop_after=2)
    assert feed.stats["book_msgs"] == 1


# ---------------------------------------------------------------------------
# date helper
# ---------------------------------------------------------------------------

def test_date_from_ns():
    from datetime import datetime, timezone
    epoch_s = int(datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc).timestamp())
    assert date_from_ns(epoch_s * 1_000_000_000) == "2026-06-18"
