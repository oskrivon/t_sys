"""Fetch Bybit 1m OHLCV klines for a symbol range -> one jsonl/coin/month.

Phase-3 Task 2 needs 1m OHLC (high/low for swing clusters + MFE path) for MAJORS,
which are too high-volume to pull as tick tape. Bybit /v5/market/kline returns full
OHLCV; the older fetcher kept only close. Disk-cached: re-runs are instant.

    python scripts/research/icebreaker_fetch_ohlc.py \
        --symbols BTCUSDT ETHUSDT --start 2026-03-01 --end 2026-04-01 --out data/klines_major_mar
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STEP_MS = 60_000  # 1m
WINDOW = 1000 * STEP_MS  # Bybit max 1000 bars/call


def to_ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch(symbol, start, end, retries=4):
    """Walk forward, 1000 bars/call. Returns ts-ascending list of OHLC dicts."""
    out = {}
    t = start
    while t < end:
        url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
               f"&interval=1&start={t}&end={min(t + WINDOW, end)}&limit=1000")
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = json.loads(r.read())
                rows = data.get("result", {}).get("list", [])
                for row in rows:  # [start, o, h, l, c, vol, turnover]
                    ts = int(row[0])
                    out[ts] = {"ts": ts, "open": float(row[1]), "high": float(row[2]),
                               "low": float(row[3]), "close": float(row[4]),
                               "volume": float(row[5])}
                break
            except Exception as e:
                if attempt == retries - 1:
                    print(f"  ! {symbol} @ {t}: {e}", flush=True)
                else:
                    time.sleep(1.5 * (attempt + 1))
        t += WINDOW
        time.sleep(0.12)  # rate-limit courtesy
    return [out[k] for k in sorted(out)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    s, e = to_ms(args.start), to_ms(args.end)
    for sym in args.symbols:
        dest = args.out / f"{sym}.jsonl"
        if dest.exists() and dest.stat().st_size > 0:
            n = sum(1 for _ in open(dest))
            print(f"[{sym}] cached ({n} bars) -> {dest}", flush=True)
            continue
        bars = fetch(sym, s, e)
        with open(dest, "w") as f:
            for b in bars:
                f.write(json.dumps(b) + "\n")
        print(f"[{sym}] {len(bars)} bars -> {dest}", flush=True)


if __name__ == "__main__":
    main()
