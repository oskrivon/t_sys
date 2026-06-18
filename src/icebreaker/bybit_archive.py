"""Bybit free historical archive -> unified parquet store.

Two free, no-auth sources:
  * Order book : quote-saver.bycsi.com/orderbook/linear/<SYM>/<date>_<SYM>_ob200.data.zip
                 (newline-delimited WS messages — same shape parse_bybit handles)
  * Trades     : public.bybit.com/trading/<SYM>/<SYM><date>.csv.gz
                 (timestamp,symbol,side,size,price,... ; side = taker aggressor)

Both are converted into the SAME partitioned parquet store the live collector
writes (`exchange=bybit/symbol=.../date=.../{book_diff,trades}`), so archive and
live data are interchangeable for replay/analysis and the store is reusable.

For archive rows there is no separate receive clock, so ``recv_ts_ns`` is derived
from the exchange timestamp (the single clock that exists for historical data).
"""
from __future__ import annotations

import csv
import json
from typing import IO, Iterator

from src.icebreaker.parse_bybit import parse_orderbook
from src.icebreaker.recorder import ParquetRecorder

OB_URL = ("https://quote-saver.bycsi.com/orderbook/linear/"
          "{sym}/{date}_{sym}_ob200.data.zip")
TRADES_URL = "https://public.bybit.com/trading/{sym}/{sym}{date}.csv.gz"


def ob_url(symbol: str, date: str) -> str:
    return OB_URL.format(sym=symbol, date=date)


def trades_url(symbol: str, date: str) -> str:
    return TRADES_URL.format(sym=symbol, date=date)


def parse_trade_csv_line(row: dict) -> tuple[int, str, float, float] | None:
    """One Bybit trades CSV row -> (exch_ts_ms, side, price, qty). None if bad."""
    try:
        ts_s = float(row["timestamp"])
        side = "buy" if row["side"].lower() == "buy" else "sell"
        price = float(row["price"])
        qty = float(row["size"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    return int(ts_s * 1000), side, price, qty


def convert_orderbook(lines: Iterator[str], recorder: ParquetRecorder,
                      symbol: str, date: str) -> int:
    """Stream OB .data lines -> book_diff parquet rows. Returns rows written."""
    n = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        ob = parse_orderbook(msg)
        if ob is None:
            continue
        recv_ns = ob.exch_ts_ms * 1_000_000
        uid = ob.update_id if ob.update_id is not None else -1
        for side, levels in (("bid", ob.bids), ("ask", ob.asks)):
            for price, qty in levels:
                recorder.record("bybit", symbol, date, "book_diff", {
                    "recv_ts_ns": recv_ns,
                    "exch_ts_ms": ob.exch_ts_ms,
                    "update_id": uid,
                    "kind": ob.kind,
                    "side": side,
                    "price": price,
                    "qty": qty,
                })
                n += 1
    return n


def convert_trades(fileobj: IO[str], recorder: ParquetRecorder,
                   symbol: str, date: str) -> int:
    """Stream a Bybit trades CSV -> trades parquet rows. Returns rows written."""
    n = 0
    reader = csv.DictReader(fileobj)
    for row in reader:
        parsed = parse_trade_csv_line(row)
        if parsed is None:
            continue
        ts_ms, side, price, qty = parsed
        recorder.record("bybit", symbol, date, "trades", {
            "recv_ts_ns": ts_ms * 1_000_000,
            "exch_ts_ms": ts_ms,
            "price": price,
            "qty": qty,
            "side": side,
        })
        n += 1
    return n
