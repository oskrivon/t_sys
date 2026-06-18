"""Unit tests for the partitioned Parquet recorder (Этап 1)."""
from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from src.icebreaker.recorder import ParquetRecorder, _safe


def _trade(ns, side="buy", price=1.0, qty=2.0):
    return {"recv_ts_ns": ns, "exch_ts_ms": ns // 1_000_000,
            "price": price, "qty": qty, "side": side}


def test_record_buffers_until_threshold(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=3)
    assert rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(1)) is False
    assert rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(2)) is False
    assert rec.buffered() == 2
    # third row hits the threshold -> flush
    assert rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(3)) is True
    assert rec.buffered() == 0
    assert rec.files_written == 1
    assert rec.rows_written == 3


def test_partition_layout_and_roundtrip(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=1000)
    for i in range(5):
        rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(i, side="sell"))
    rec.flush_all()

    part_dir = (tmp_path / "exchange=bybit" / "symbol=FOO-USDT_USDT"
                / "date=2026-06-18" / "trades")
    files = list(part_dir.glob("part-*.parquet"))
    assert len(files) == 1

    table = pq.read_table(files[0])
    assert table.num_rows == 5
    assert table.column("side").to_pylist() == ["sell"] * 5
    # The data columns are present (pyarrow may also recover exchange/symbol/date
    # from the Hive partition path — that's fine, just check our schema is a subset).
    assert set(["recv_ts_ns", "exch_ts_ms", "price", "qty", "side"]).issubset(table.schema.names)


def test_each_flush_writes_new_part_file(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=2)
    for i in range(4):  # -> two flushes of 2
        rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(i))
    part_dir = (tmp_path / "exchange=bybit" / "symbol=FOO-USDT_USDT"
                / "date=2026-06-18" / "trades")
    files = sorted(p.name for p in part_dir.glob("part-*.parquet"))
    assert files == ["part-000000.parquet", "part-000001.parquet"]


def test_streams_partitioned_independently(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=1000)
    rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(1))
    rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "book_diff", {
        "recv_ts_ns": 1, "exch_ts_ms": 0, "update_id": 5, "kind": "delta",
        "side": "bid", "price": 1.0, "qty": 3.0})
    rec.record("mexc", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(2))
    rec.flush_all()

    assert (tmp_path / "exchange=bybit" / "symbol=FOO-USDT_USDT"
            / "date=2026-06-18" / "trades").exists()
    assert (tmp_path / "exchange=bybit" / "symbol=FOO-USDT_USDT"
            / "date=2026-06-18" / "book_diff").exists()
    assert (tmp_path / "exchange=mexc" / "symbol=FOO-USDT_USDT"
            / "date=2026-06-18" / "trades").exists()


def test_date_partition_rotation(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=1000)
    rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(1))
    rec.record("bybit", "FOO/USDT:USDT", "2026-06-19", "trades", _trade(2))
    rec.flush_all()
    base = tmp_path / "exchange=bybit" / "symbol=FOO-USDT_USDT"
    assert (base / "date=2026-06-18" / "trades").exists()
    assert (base / "date=2026-06-19" / "trades").exists()


def test_book_diff_schema_roundtrip(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=1000)
    rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "book_diff", {
        "recv_ts_ns": 111, "exch_ts_ms": 0, "update_id": 7, "kind": "snapshot",
        "side": "ask", "price": 2.5, "qty": 100.0})
    rec.flush_all()
    f = next((tmp_path).rglob("book_diff/part-*.parquet"))
    t = pq.read_table(f)
    assert t.column("kind").to_pylist() == ["snapshot"]
    assert t.column("update_id").to_pylist() == [7]


def test_unknown_kind_raises(tmp_path):
    rec = ParquetRecorder(tmp_path)
    with pytest.raises(ValueError):
        rec.record("bybit", "FOO", "2026-06-18", "bogus", {})


def test_flush_all_returns_count_and_empties(tmp_path):
    rec = ParquetRecorder(tmp_path, flush_rows=1000)
    for i in range(7):
        rec.record("bybit", "FOO/USDT:USDT", "2026-06-18", "trades", _trade(i))
    assert rec.flush_all() == 7
    assert rec.buffered() == 0
    assert rec.flush_all() == 0  # nothing left


def test_safe_symbol():
    assert _safe("BTC/USDT:USDT") == "BTC-USDT_USDT"
