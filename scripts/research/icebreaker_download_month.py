"""Download a month of free Bybit archive (order book + trades) into the unified
parquet store. Reusable: same layout as the live collector.

    python scripts/research/icebreaker_download_month.py \
        --symbols TAOUSDT SIRENUSDT ZECUSDT SUIUSDT \
        --start 2026-03-01 --end 2026-03-31 \
        --out /root/trading/data/icebreaker

Idempotent: skips a (symbol, date, kind) whose partition already exists. Raw
zip/gz files are streamed and deleted after conversion.
"""
from __future__ import annotations

import argparse
import gzip
import io
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.icebreaker.bybit_archive import (
    convert_orderbook,
    convert_trades,
    ob_url,
    trades_url,
)
from src.icebreaker.recorder import ParquetRecorder


def daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    d = d0
    while d <= d1:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _partition_done(out: Path, symbol: str, date_str: str, kind: str) -> bool:
    p = out / "exchange=bybit" / f"symbol={symbol}" / f"date={date_str}" / kind
    return p.exists() and any(p.glob("part-*.parquet"))


def _download(url: str, timeout: int = 180) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "icebreaker/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print(f"    [download fail] {url.rsplit('/', 1)[-1]}: {e}")
        return None


def fetch_orderbook(out: Path, symbol: str, date_str: str) -> int:
    if _partition_done(out, symbol, date_str, "book_diff"):
        print(f"    book_diff exists, skip")
        return 0
    raw = _download(ob_url(symbol, date_str))
    if raw is None:
        return 0
    rec = ParquetRecorder(out, flush_rows=200_000)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8")
            n = convert_orderbook(text, rec, symbol, date_str)
    rec.flush_all()
    return n


def fetch_trades(out: Path, symbol: str, date_str: str) -> int:
    if _partition_done(out, symbol, date_str, "trades"):
        print(f"    trades exists, skip")
        return 0
    raw = _download(trades_url(symbol, date_str))
    if raw is None:
        return 0
    rec = ParquetRecorder(out, flush_rows=500_000)
    with gzip.open(io.BytesIO(raw), "rt") as fh:
        n = convert_trades(fh, rec, symbol, date_str)
    rec.flush_all()
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", type=Path, default=ROOT / "data" / "icebreaker")
    p.add_argument("--skip-trades", action="store_true")
    p.add_argument("--skip-orderbook", action="store_true")
    args = p.parse_args()

    dates = list(daterange(args.start, args.end))
    print(f"symbols={args.symbols} dates={dates[0]}..{dates[-1]} ({len(dates)}) "
          f"out={args.out}")
    for symbol in args.symbols:
        for d in dates:
            print(f"[{symbol} {d}]")
            if not args.skip_orderbook:
                nb = fetch_orderbook(args.out, symbol, d)
                if nb:
                    print(f"    book_diff rows: {nb:,}")
            if not args.skip_trades:
                nt = fetch_trades(args.out, symbol, d)
                if nt:
                    print(f"    trades rows: {nt:,}")
    print("done")


if __name__ == "__main__":
    main()
