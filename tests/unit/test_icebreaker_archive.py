"""Unit tests for Bybit archive -> unified parquet conversion (month pipeline)."""
from __future__ import annotations

import io
import json

import pyarrow.parquet as pq

from src.icebreaker.bybit_archive import (
    convert_orderbook,
    convert_trades,
    ob_url,
    parse_trade_csv_line,
    trades_url,
)
from src.icebreaker.recorder import ParquetRecorder


def test_url_builders():
    assert ob_url("TAOUSDT", "2026-03-03") == (
        "https://quote-saver.bycsi.com/orderbook/linear/"
        "TAOUSDT/2026-03-03_TAOUSDT_ob200.data.zip")
    assert trades_url("TAOUSDT", "2026-03-03") == (
        "https://public.bybit.com/trading/TAOUSDT/TAOUSDT2026-03-03.csv.gz")


def test_parse_trade_csv_line():
    row = {"timestamp": "1781568000.2812", "symbol": "TAOUSDT", "side": "Sell",
           "size": "0.713", "price": "266.27"}
    assert parse_trade_csv_line(row) == (1781568000281, "sell", 266.27, 0.713)
    row["side"] = "Buy"
    assert parse_trade_csv_line(row)[1] == "buy"


def test_parse_trade_csv_line_bad_row():
    assert parse_trade_csv_line({"timestamp": "x", "side": "Buy",
                                 "size": "1", "price": "1"}) is None
    assert parse_trade_csv_line({}) is None


def _read(tmp_path, kind, sym="TAOUSDT"):
    base = tmp_path / "exchange=bybit" / f"symbol={sym}" / "date=2026-03-03" / kind
    return pq.read_table(sorted(base.glob("part-*.parquet")))


def test_convert_orderbook_to_parquet(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=10_000)
    lines = [
        json.dumps({"topic": "orderbook.200.TAOUSDT", "type": "snapshot", "ts": 1000,
                     "data": {"s": "TAOUSDT", "b": [["100.0", "5"], ["99.0", "3"]],
                              "a": [["101.0", "4"]], "u": 1}}),
        json.dumps({"topic": "orderbook.200.TAOUSDT", "type": "delta", "ts": 1100,
                     "data": {"s": "TAOUSDT", "b": [["100.0", "0"]], "a": [], "u": 2}}),
        "",  # blank line tolerated
        "not json",  # bad line tolerated
    ]
    n = convert_orderbook(iter(lines), rec, "TAOUSDT", "2026-03-03")
    rec.flush_all()
    assert n == 4  # 2 bids + 1 ask (snapshot) + 1 bid (delta)
    t = _read(tmp_path, "book_diff")
    assert t.num_rows == 4
    assert set(t.column("kind").to_pylist()) == {"snapshot", "delta"}
    # recv_ts derived from exch ts (single clock for archive)
    assert t.column("recv_ts_ns").to_pylist()[0] == 1000 * 1_000_000


def test_convert_trades_to_parquet(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=10_000)
    csv_text = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID\n"
        "1781568000.2812,TAOUSDT,Sell,0.713,266.27,ZeroPlusTick,a\n"
        "1781568000.5000,TAOUSDT,Buy,1.5,266.30,PlusTick,b\n"
    )
    n = convert_trades(io.StringIO(csv_text), rec, "TAOUSDT", "2026-03-03")
    rec.flush_all()
    assert n == 2
    t = _read(tmp_path, "trades")
    assert t.column("side").to_pylist() == ["sell", "buy"]
    assert t.column("price").to_pylist() == [266.27, 266.30]
    assert t.column("qty").to_pylist() == [0.713, 1.5]
