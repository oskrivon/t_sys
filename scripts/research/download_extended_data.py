"""Download 12 months of data for extended symbol set.

Downloads 4H, 1D (and 1H for existing symbols) going back 12 months.
Adds 8 new liquid altcoins to the existing 12.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import ccxt

DATA = ROOT / "data" / "processed" / "candles"

# Existing 12
EXISTING = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

# New 8 (liquid, popular alts)
NEW_SYMBOLS = [
    "AAVE/USDT", "UNI/USDT", "INJ/USDT", "TIA/USDT",
    "WIF/USDT", "ONDO/USDT", "RENDER/USDT", "FET/USDT",
]

ALL_SYMBOLS = EXISTING + NEW_SYMBOLS

MONTHS = 12


def fetch_ohlcv(exchange, symbol: str, tf: str, months: int) -> pd.DataFrame:
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c, cur = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except Exception as e:
            print(f" error: {e}")
            break
        if not c:
            break
        all_c.extend(c)
        last = c[-1][0]
        if last <= cur or len(c) < 1000:
            break
        cur = last + 1
        time.sleep(0.12)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    exchange = ccxt.binance({"enableRateLimit": True})

    timeframes = ["4h", "1d"]

    total = len(ALL_SYMBOLS) * len(timeframes)
    done = 0

    for symbol in ALL_SYMBOLS:
        key = symbol.replace("/", "")
        for tf in timeframes:
            done += 1
            path = DATA / f"{key}_{tf}.parquet"

            # Check if we already have 12 months
            if path.exists():
                existing = pd.read_parquet(path)
                days = (existing["ts"].iloc[-1] - existing["ts"].iloc[0]).days
                if days >= 350:
                    print(f"[{done}/{total}] {symbol} {tf}: already {days}d, skip")
                    continue
                else:
                    print(f"[{done}/{total}] {symbol} {tf}: only {days}d, re-downloading...")
            else:
                print(f"[{done}/{total}] {symbol} {tf}: downloading...", end="")

            df = fetch_ohlcv(exchange, symbol, tf, MONTHS)
            if not df.empty:
                df.to_parquet(path, index=False)
                days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).days
                print(f" {len(df)} candles, {days}d")
            else:
                print(f" FAILED")

    # Summary
    print(f"\n=== Summary ===")
    for tf in timeframes:
        print(f"\n{tf}:")
        for symbol in ALL_SYMBOLS:
            key = symbol.replace("/", "")
            path = DATA / f"{key}_{tf}.parquet"
            if path.exists():
                df = pd.read_parquet(path)
                days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).days
                print(f"  {symbol:14s} {len(df):5d} candles, {days:3d} days")
            else:
                print(f"  {symbol:14s} MISSING")


if __name__ == "__main__":
    main()
