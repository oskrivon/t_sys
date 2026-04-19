"""Download 24 months of data for top 50 liquid symbols.

Filters out stablecoins, wrapped tokens, and fan tokens.
Downloads 4H and 1D timeframes.
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

# Top 50 liquid USDT pairs (excluding stablecoins, EUR, fan tokens)
SYMBOLS = [
    # Tier 1: >$50M daily
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT",
    # Tier 2: $10-50M
    "TRX/USDT", "LINK/USDT", "ADA/USDT", "AVAX/USDT", "NEAR/USDT",
    "LTC/USDT", "FET/USDT", "UNI/USDT",
    # Tier 3: $5-10M
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    # Tier 4: $2-5M (still liquid enough)
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "WIF/USDT", "ONDO/USDT", "RENDER/USDT",
    # Extra good alts
    "ATOM/USDT", "ETC/USDT", "FTM/USDT", "MATIC/USDT", "APT/USDT",
    "MANTA/USDT", "SEI/USDT", "JUP/USDT", "WLD/USDT", "STRK/USDT",
    "PENDLE/USDT", "ENA/USDT", "TAO/USDT", "GALA/USDT",
]

MONTHS = 24


def fetch_ohlcv(exchange, symbol: str, tf: str, months: int) -> pd.DataFrame:
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c, cur = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except Exception as e:
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
    total = len(SYMBOLS) * len(timeframes)
    done = 0
    skipped = 0

    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        for tf in timeframes:
            done += 1
            path = DATA / f"{key}_{tf}.parquet"

            # Check if we already have enough
            if path.exists():
                existing = pd.read_parquet(path)
                days = (existing["ts"].iloc[-1] - existing["ts"].iloc[0]).days
                if days >= 700:  # ~24 months
                    skipped += 1
                    continue

            sys.stdout.write(f"[{done}/{total}] {symbol} {tf}...")
            sys.stdout.flush()

            df = fetch_ohlcv(exchange, symbol, tf, MONTHS)
            if not df.empty:
                df.to_parquet(path, index=False)
                days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).days
                print(f" {len(df)} candles, {days}d")
            else:
                print(f" FAILED (may not exist on Binance)")

    # Summary
    print(f"\nDone. Skipped {skipped} already complete.")
    print(f"\n{'Symbol':16s} {'4H candles':>12} {'Days':>6}")
    count = 0
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_4h.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).days
            print(f"{symbol:16s} {len(df):>12} {days:>6}")
            count += 1
    print(f"\nTotal: {count} symbols with 4H data")


if __name__ == "__main__":
    main()
