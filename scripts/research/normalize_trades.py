"""
Нормализовать raw trades со всех трёх бирж в единый per-second формат.

Выход (per-second агрегат в parquet):
    second_ms   — unix seconds * 1000 (начало секундной ячейки)
    bid_proxy   — max(price | side=sell) — приближение верха bid
    ask_proxy   — min(price | side=buy)  — приближение низа ask
    sell_n, buy_n       — число сделок в секунду
    sell_vol, buy_vol   — объём base asset
    last_price          — последняя сделка
    last_side           — её сторона

Файл результата:
    data/processed/arb_research/{exchange}_{symbol}_{month}.parquet

Пример:
    python scripts/research/normalize_trades.py --month 2026-03
"""
from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd

RAW = Path("data/raw")
OUT = Path("data/processed/arb_research")
CHUNK_ROWS = 1_000_000


def human_dur(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m{int(sec % 60):02d}s"


def aggregate_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """
    chunk: columns [second_ms, price, qty, side]  side in {buy, sell}
    Возвращает per-second агрегат.
    """
    # разделяем по стороне — нужны разные статистики
    sells = chunk[chunk["side"] == "sell"]
    buys = chunk[chunk["side"] == "buy"]

    # bid_proxy = max(sell_price), ask_proxy = min(buy_price)
    sell_agg = sells.groupby("second_ms").agg(
        bid_proxy=("price", "max"),
        sell_n=("price", "size"),
        sell_vol=("qty", "sum"),
    )
    buy_agg = buys.groupby("second_ms").agg(
        ask_proxy=("price", "min"),
        buy_n=("price", "size"),
        buy_vol=("qty", "sum"),
    )
    combined = sell_agg.join(buy_agg, how="outer")

    # последняя сделка в секунде (для forward-fill позже)
    last = chunk.sort_values("idx").groupby("second_ms").tail(1)
    last = last.set_index("second_ms")[["price", "side"]]
    last.columns = ["last_price", "last_side"]
    combined = combined.join(last, how="outer")

    return combined.reset_index()


def merge_accum(accum: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Склеить агрегаты из двух чанков (могут иметь общие секунды)."""
    if accum is None:
        return new
    combined = pd.concat([accum, new], ignore_index=True)
    out = combined.groupby("second_ms").agg(
        bid_proxy=("bid_proxy", "max"),
        ask_proxy=("ask_proxy", "min"),
        sell_n=("sell_n", "sum"),
        buy_n=("buy_n", "sum"),
        sell_vol=("sell_vol", "sum"),
        buy_vol=("buy_vol", "sum"),
        last_price=("last_price", "last"),  # приближённо — лучше чем ничего
        last_side=("last_side", "last"),
    ).reset_index()
    return out


def normalize_binance(zip_path: Path, symbol: str) -> pd.DataFrame:
    """
    Binance aggTrades CSV (нет заголовка):
        id, price, qty, first_id, last_id, timestamp, is_buyer_maker, is_best_match
    timestamp может быть в ms (13 цифр) или μs (16 цифр) — детектим по величине.
    is_buyer_maker=True  -> taker sold (bid hit) -> side=sell
    is_buyer_maker=False -> taker bought (ask hit) -> side=buy
    """
    t0 = time.time()
    accum: pd.DataFrame | None = None
    row_count = 0

    with zipfile.ZipFile(zip_path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            reader = pd.read_csv(
                f,
                header=None,
                names=[
                    "id", "price", "qty",
                    "first_id", "last_id", "ts",
                    "is_buyer_maker", "is_best_match",
                ],
                usecols=["price", "qty", "ts", "is_buyer_maker"],
                dtype={"price": "float64", "qty": "float64", "ts": "int64"},
                chunksize=CHUNK_ROWS,
            )
            for i, chunk in enumerate(reader):
                # auto-detect μs vs ms
                if chunk["ts"].iloc[0] > 10**14:
                    chunk["ts_ms"] = chunk["ts"] // 1000
                else:
                    chunk["ts_ms"] = chunk["ts"]

                chunk["second_ms"] = (chunk["ts_ms"] // 1000) * 1000
                chunk["side"] = chunk["is_buyer_maker"].map(
                    {True: "sell", False: "buy"}
                ).fillna("buy")
                chunk["idx"] = chunk.index + row_count

                agg = aggregate_chunk(
                    chunk[["second_ms", "price", "qty", "side", "idx"]]
                )
                accum = merge_accum(accum, agg)
                row_count += len(chunk)
                sys.stdout.write(
                    f"\r  binance {symbol}: chunk {i+1}, rows {row_count:>12,}, "
                    f"seconds {len(accum):>7,}, {human_dur(time.time()-t0)}     "
                )
                sys.stdout.flush()
    sys.stdout.write("\n")
    assert accum is not None
    return accum.sort_values("second_ms").reset_index(drop=True)


def normalize_bybit(gz_path: Path, symbol: str) -> pd.DataFrame:
    """
    Bybit spot CSV: заголовок 5 колонок ('id,timestamp,price,volume,side'),
    но в строках данных 6 полей (последнее — неназванный флаг 0/1).
    Парсим как 6-колоночный без header.
    timestamp: ms. side: buy/sell (taker side).
    """
    t0 = time.time()
    accum: pd.DataFrame | None = None
    row_count = 0

    reader = pd.read_csv(
        gz_path,
        compression="gzip",
        header=0,  # пропустить оригинальный заголовок
        names=["id", "timestamp", "price", "volume", "side", "flag"],
        usecols=["timestamp", "price", "volume", "side"],
        dtype={"price": "float64", "volume": "float64", "timestamp": "int64"},
        chunksize=CHUNK_ROWS,
    )
    for i, chunk in enumerate(reader):
        chunk["side"] = chunk["side"].str.lower()
        chunk["second_ms"] = (chunk["timestamp"] // 1000) * 1000
        chunk["qty"] = chunk["volume"]
        chunk["idx"] = chunk.index + row_count

        agg = aggregate_chunk(
            chunk[["second_ms", "price", "qty", "side", "idx"]]
        )
        accum = merge_accum(accum, agg)
        row_count += len(chunk)
        sys.stdout.write(
            f"\r  bybit {symbol}: chunk {i+1}, rows {row_count:>12,}, "
            f"seconds {len(accum):>7,}, {human_dur(time.time()-t0)}     "
        )
        sys.stdout.flush()
    sys.stdout.write("\n")
    assert accum is not None
    return accum.sort_values("second_ms").reset_index(drop=True)


def normalize_okx(dir_path: Path, symbol: str, month: str) -> pd.DataFrame:
    """
    OKX daily zips, каждый — CSV с заголовком:
        instrument_name, trade_id, side, price, size, created_time(ms)
    """
    t0 = time.time()
    accum: pd.DataFrame | None = None
    row_count = 0

    files = sorted(dir_path.glob(f"{symbol}-trades-{month}-*.zip"))
    for fi, zip_path in enumerate(files):
        with zipfile.ZipFile(zip_path) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as f:
                reader = pd.read_csv(
                    f,
                    usecols=["side", "price", "size", "created_time"],
                    dtype={
                        "price": "float64",
                        "size": "float64",
                        "created_time": "int64",
                    },
                    chunksize=CHUNK_ROWS,
                )
                for chunk in reader:
                    chunk["side"] = chunk["side"].str.lower()
                    chunk["second_ms"] = (chunk["created_time"] // 1000) * 1000
                    chunk["qty"] = chunk["size"]
                    chunk["idx"] = chunk.index + row_count

                    agg = aggregate_chunk(
                        chunk[["second_ms", "price", "qty", "side", "idx"]]
                    )
                    accum = merge_accum(accum, agg)
                    row_count += len(chunk)
        sys.stdout.write(
            f"\r  okx {symbol}: file {fi+1}/{len(files)}, rows {row_count:>12,}, "
            f"seconds {len(accum):>7,}, {human_dur(time.time()-t0)}     "
        )
        sys.stdout.flush()
    sys.stdout.write("\n")
    assert accum is not None
    return accum.sort_values("second_ms").reset_index(drop=True)


def save(df: pd.DataFrame, exchange: str, symbol: str, month: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{exchange}_{symbol}_{month}.parquet"
    df.to_parquet(out, index=False)
    return out


def run_all(month: str) -> None:
    jobs = [
        ("binance", "BTCUSDT", RAW / "binance/BTCUSDT" / f"BTCUSDT-aggTrades-{month}.zip"),
        ("binance", "ETHUSDT", RAW / "binance/ETHUSDT" / f"ETHUSDT-aggTrades-{month}.zip"),
        ("bybit",   "BTCUSDT", RAW / "bybit/BTCUSDT"   / f"BTCUSDT-{month}.csv.gz"),
        ("bybit",   "ETHUSDT", RAW / "bybit/ETHUSDT"   / f"ETHUSDT-{month}.csv.gz"),
        ("okx",     "BTC-USDT", RAW / "okx/BTC-USDT"),
        ("okx",     "ETH-USDT", RAW / "okx/ETH-USDT"),
    ]

    for exchange, symbol, src in jobs:
        print(f"\n=== {exchange} {symbol} ===")
        out_path = OUT / f"{exchange}_{symbol}_{month}.parquet"
        if out_path.exists():
            print(f"  already done: {out_path}")
            continue

        if exchange == "binance":
            if not src.exists():
                print(f"  SKIP: нет файла {src}")
                continue
            df = normalize_binance(src, symbol)
        elif exchange == "bybit":
            if not src.exists():
                print(f"  SKIP: нет файла {src}")
                continue
            df = normalize_bybit(src, symbol)
        elif exchange == "okx":
            if not src.exists():
                print(f"  SKIP: нет директории {src}")
                continue
            df = normalize_okx(src, symbol, month)
        else:
            continue

        out = save(df, exchange, symbol, month)
        print(
            f"  saved: {out} "
            f"({len(df):,} seconds, {out.stat().st_size / 1e6:.1f} MB)"
        )
        # quick sanity
        print(
            f"  sample: bid_proxy range "
            f"[{df['bid_proxy'].min():.2f} .. {df['bid_proxy'].max():.2f}], "
            f"ask_proxy range [{df['ask_proxy'].min():.2f} .. {df['ask_proxy'].max():.2f}]"
        )


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM (напр. 2026-03)")
    args = ap.parse_args()
    run_all(args.month)


if __name__ == "__main__":
    cli()
