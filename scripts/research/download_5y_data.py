"""Download 5 years of OHLCV data for validation pipeline.

Downloads from Binance (free, no API key needed for public data).
BTC available since 2017, most alts since 2020-2021.

Saves to data/processed/candles/{SYMBOL}_{tf}.parquet
Merges with existing data (no duplicates).

Usage:
    python scripts/research/download_5y_data.py
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

# All symbols used across strategies
SYMBOLS = [
    # Core (Weekend, Miro, VR all use these)
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT",
    # VR + Miro universe
    "TRX/USDT", "LINK/USDT", "ADA/USDT", "AVAX/USDT", "NEAR/USDT",
    "LTC/USDT", "FET/USDT", "UNI/USDT",
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "WIF/USDT", "ONDO/USDT", "RENDER/USDT",
    "ATOM/USDT", "ETC/USDT", "FTM/USDT", "MATIC/USDT", "APT/USDT",
    "MANTA/USDT", "SEI/USDT", "JUP/USDT", "WLD/USDT", "STRK/USDT",
    "PENDLE/USDT", "ENA/USDT", "TAO/USDT", "GALA/USDT",
]

MONTHS = 60  # 5 years
MIN_DAYS_SKIP = 1800  # skip if already have 5 years


def fetch_ohlcv(exchange, symbol: str, tf: str, months: int) -> pd.DataFrame:
    """Fetch OHLCV with pagination."""
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c: list = []
    cur = since_ms

    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except ccxt.BadSymbol:
            break
        except Exception as e:
            print(f" error: {e}")
            time.sleep(1)
            continue

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


def merge_data(existing_path: Path, new_df: pd.DataFrame) -> pd.DataFrame:
    """Merge existing parquet with new data, remove duplicates."""
    if existing_path.exists():
        old = pd.read_parquet(existing_path)
        old["ts"] = pd.to_datetime(old["ts"], utc=True)
        combined = pd.concat([old, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        return combined
    return new_df


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    exchange = ccxt.binance({"enableRateLimit": True})

    timeframes = ["4h", "1d"]
    total = len(SYMBOLS) * len(timeframes)
    done = 0
    downloaded = 0
    skipped = 0

    print(f"Downloading {MONTHS} months of data for {len(SYMBOLS)} symbols")
    print(f"Timeframes: {timeframes}")
    print(f"Target: data/processed/candles/")
    print()

    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        for tf in timeframes:
            done += 1
            path = DATA / f"{key}_{tf}.parquet"

            # Check existing
            if path.exists():
                existing = pd.read_parquet(path)
                days = (existing["ts"].iloc[-1] - existing["ts"].iloc[0]).days
                if days >= MIN_DAYS_SKIP:
                    skipped += 1
                    continue

            sys.stdout.write(f"\r[{done}/{total}] {symbol} {tf}...")
            sys.stdout.flush()

            df = fetch_ohlcv(exchange, symbol, tf, MONTHS)
            if not df.empty:
                merged = merge_data(path, df)
                merged.to_parquet(path, index=False)
                days = (merged["ts"].iloc[-1] - merged["ts"].iloc[0]).days
                downloaded += 1
                print(f"\r[{done}/{total}] {symbol} {tf}: {len(merged)} candles, {days}d")
            else:
                print(f"\r[{done}/{total}] {symbol} {tf}: no data (may not be listed)")

    # Summary
    print(f"\n{'='*60}")
    print(f"Done. Downloaded: {downloaded}, Skipped: {skipped}")
    print(f"\nData coverage:")
    print(f"{'Symbol':<14} {'4h candles':>12} {'4h days':>8} {'1d candles':>12} {'1d days':>8}")
    print("-" * 60)

    for symbol in SYMBOLS[:10]:  # show first 10
        key = symbol.replace("/", "")
        parts = []
        for tf in timeframes:
            p = DATA / f"{key}_{tf}.parquet"
            if p.exists():
                d = pd.read_parquet(p)
                days = (d["ts"].iloc[-1] - d["ts"].iloc[0]).days
                parts.extend([f"{len(d):>12}", f"{days:>8}"])
            else:
                parts.extend([f"{'---':>12}", f"{'---':>8}"])
        print(f"{symbol:<14} {parts[0]} {parts[1]} {parts[2]} {parts[3]}")

    # BTC range
    btc_1d = DATA / "BTCUSDT_1d.parquet"
    if btc_1d.exists():
        df = pd.read_parquet(btc_1d)
        print(f"\nBTC range: {df['ts'].iloc[0]} -- {df['ts'].iloc[-1]}")
        print(f"Total: {(df['ts'].iloc[-1] - df['ts'].iloc[0]).days} days = {(df['ts'].iloc[-1] - df['ts'].iloc[0]).days/365:.1f} years")


if __name__ == "__main__":
    main()
