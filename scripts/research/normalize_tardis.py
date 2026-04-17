"""
Нормализовать Tardis book_ticker tick-level данные в per-second агрегат.

Tardis schema:
    exchange, symbol, timestamp(μs), local_timestamp(μs),
    ask_amount, ask_price, bid_price, bid_amount

Выход (per-second parquet), колонки:
    second_ms, max_bid, min_ask, last_bid, last_ask, n_updates

Сохраняем как:
    data/processed/arb_research/tardis_{exchange}_{symbol}_{YYYY-MM-DD}.parquet

Пример:
    python scripts/research/normalize_tardis.py --date 2026-03-01
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

RAW = Path("data/raw/tardis")
OUT = Path("data/processed/arb_research")
CHUNK_ROWS = 1_000_000

# (display_exchange, symbol)
JOBS = [
    ("binance", "BTCUSDT"),
    ("binance", "ETHUSDT"),
    ("bybit",   "BTCUSDT"),
    ("bybit",   "ETHUSDT"),
    ("okx",     "BTC-USDT"),
    ("okx",     "ETH-USDT"),
]


def aggregate_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """chunk: [second_ms, bid_price, ask_price, idx] → per-second агрегат"""
    grouped = chunk.groupby("second_ms")
    out = grouped.agg(
        max_bid=("bid_price", "max"),
        min_ask=("ask_price", "min"),
        last_bid=("bid_price", "last"),
        last_ask=("ask_price", "last"),
        n_updates=("bid_price", "size"),
    )
    # last_bid/last_ask в chunk — нужно брать РЕАЛЬНО последний по idx
    # groupby().last() берёт последнюю строку в порядке появления — у нас уже отсортировано по idx.
    return out.reset_index()


def merge_accum(accum: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Склеить два агрегата: для overlap-секунд комбинируем min/max/last/sum."""
    if accum is None:
        return new
    combined = pd.concat([accum, new], ignore_index=True)
    # для last_* берём просто last — порядок уже сохранён (новые чанки приходят позже)
    out = combined.groupby("second_ms").agg(
        max_bid=("max_bid", "max"),
        min_ask=("min_ask", "min"),
        last_bid=("last_bid", "last"),
        last_ask=("last_ask", "last"),
        n_updates=("n_updates", "sum"),
    ).reset_index()
    return out


def normalize_file(path: Path, exchange: str, symbol: str) -> pd.DataFrame:
    t0 = time.time()
    accum: pd.DataFrame | None = None
    row_count = 0

    reader = pd.read_csv(
        path,
        compression="gzip",
        usecols=["timestamp", "bid_price", "ask_price"],
        dtype={"timestamp": "int64", "bid_price": "float64", "ask_price": "float64"},
        chunksize=CHUNK_ROWS,
    )
    for i, chunk in enumerate(reader):
        # timestamp в μs -> second_ms
        chunk["second_ms"] = (chunk["timestamp"] // 1_000_000) * 1000
        chunk["idx"] = chunk.index + row_count

        agg = aggregate_chunk(chunk[["second_ms", "bid_price", "ask_price", "idx"]])
        accum = merge_accum(accum, agg)
        row_count += len(chunk)
        sys.stdout.write(
            f"\r  {exchange} {symbol}: chunk {i+1}, rows {row_count:>12,}, "
            f"seconds {len(accum):>6,}, {time.time()-t0:5.1f}s     "
        )
        sys.stdout.flush()
    sys.stdout.write("\n")
    assert accum is not None
    return accum.sort_values("second_ms").reset_index(drop=True)


def run(date: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"=== Tardis normalize, {date} ===")

    for exchange, symbol in JOBS:
        src = RAW / exchange / symbol / f"{symbol}-book_ticker-{date}.csv.gz"
        out_path = OUT / f"tardis_{exchange}_{symbol}_{date}.parquet"
        print(f"\n[{exchange} {symbol}]")
        if out_path.exists():
            print(f"  already done: {out_path}")
            continue
        if not src.exists():
            print(f"  SKIP: нет файла {src}")
            continue

        df = normalize_file(src, exchange, symbol)
        df.to_parquet(out_path, index=False)
        print(
            f"  saved: {out_path} ({len(df):,} seconds, "
            f"{out_path.stat().st_size / 1e6:.1f} MB)"
        )
        print(
            f"  sample: max_bid [{df['max_bid'].min():.2f} .. {df['max_bid'].max():.2f}], "
            f"min_ask [{df['min_ask'].min():.2f} .. {df['min_ask'].max():.2f}]"
        )


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (напр. 2026-03-01)")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    cli()
