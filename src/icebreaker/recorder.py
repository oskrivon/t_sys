"""Buffered, partitioned Parquet recorder for the icebreaker collector.

Streams of `trades`, `book_diff` and `book_snap` rows are buffered in memory and
flushed to part files under a Hive-style partition layout:

    {base_dir}/exchange={ex}/symbol={sym}/date={YYYY-MM-DD}/{kind}/part-NNNNNN.parquet

Each flush writes one new part file (Parquet is immutable, so we never append to
an existing file). Daily rotation is implicit in the ``date=`` partition. A
closed (yesterday's) partition can be handed to the offload step.

Flushing is driven by the caller (size threshold via ``record`` return value, or
a periodic ``flush_all``) so the recorder stays free of any wall clock — every
row carries the timestamps the collector stamped on receipt.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pyarrow as pa
import pyarrow.parquet as pq

# Explicit schemas keep column types stable across flushes (so the part files of
# one partition stay readable as a single dataset).
SCHEMAS: dict[str, pa.Schema] = {
    "trades": pa.schema([
        ("recv_ts_ns", pa.int64()),
        ("exch_ts_ms", pa.int64()),
        ("price", pa.float64()),
        ("qty", pa.float64()),
        ("side", pa.string()),       # "buy" | "sell" (aggressor)
    ]),
    "book_diff": pa.schema([
        ("recv_ts_ns", pa.int64()),
        ("exch_ts_ms", pa.int64()),
        ("update_id", pa.int64()),
        ("kind", pa.string()),       # "snapshot" | "delta"
        ("side", pa.string()),       # "bid" | "ask"
        ("price", pa.float64()),
        ("qty", pa.float64()),       # 0 => remove level
    ]),
    "book_snap": pa.schema([
        ("recv_ts_ns", pa.int64()),
        ("exch_ts_ms", pa.int64()),
        ("side", pa.string()),
        ("price", pa.float64()),
        ("qty", pa.float64()),
    ]),
}


@dataclass(frozen=True)
class _Key:
    exchange: str
    symbol: str
    date: str
    kind: str


def _safe(component: str) -> str:
    """Make a symbol filesystem-safe (BTC/USDT:USDT -> BTC-USDT_USDT)."""
    return component.replace("/", "-").replace(":", "_")


class ParquetRecorder:
    def __init__(self, base_dir: str | Path, flush_rows: int = 5_000) -> None:
        self.base_dir = Path(base_dir)
        self.flush_rows = flush_rows
        self._buffers: dict[_Key, list[dict]] = defaultdict(list)
        self._part_seq: dict[_Key, int] = defaultdict(int)
        self.rows_written = 0
        self.files_written = 0

    def record(self, exchange: str, symbol: str, date: str, kind: str,
               row: Mapping) -> bool:
        """Buffer one row. Returns True if a flush was triggered."""
        if kind not in SCHEMAS:
            raise ValueError(f"unknown kind {kind!r}")
        key = _Key(exchange, symbol, date, kind)
        self._buffers[key].append(dict(row))
        if len(self._buffers[key]) >= self.flush_rows:
            self._flush_key(key)
            return True
        return False

    def buffered(self) -> int:
        """Total rows currently buffered (across all streams)."""
        return sum(len(v) for v in self._buffers.values())

    def flush_all(self) -> int:
        """Flush every non-empty buffer. Returns rows flushed."""
        n = 0
        for key in list(self._buffers.keys()):
            n += self._flush_key(key)
        return n

    def close(self) -> int:
        return self.flush_all()

    # ------------------------------------------------------------------
    def _partition_dir(self, key: _Key) -> Path:
        return (
            self.base_dir
            / f"exchange={key.exchange}"
            / f"symbol={_safe(key.symbol)}"
            / f"date={key.date}"
            / key.kind
        )

    def _flush_key(self, key: _Key) -> int:
        rows = self._buffers.get(key)
        if not rows:
            return 0
        schema = SCHEMAS[key.kind]
        cols = {name: [r.get(name) for r in rows] for name in schema.names}
        table = pa.table(cols, schema=schema)

        part_dir = self._partition_dir(key)
        part_dir.mkdir(parents=True, exist_ok=True)
        seq = self._part_seq[key]
        self._part_seq[key] = seq + 1
        path = part_dir / f"part-{seq:06d}.parquet"
        pq.write_table(table, path, compression="zstd")

        n = len(rows)
        self.rows_written += n
        self.files_written += 1
        self._buffers[key] = []
        return n
