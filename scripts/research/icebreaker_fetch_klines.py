"""Fetch Bybit 5min close prices for the icebreaker coins (to anchor OI->liq map).

The liquidation-cluster map needs price(t) at each OI sample so we know WHERE each
OI increase opened (and thus where it liquidates). Bybit /v5/market/kline, 5min,
newest-first <=1000 per call, walked forward. Output: one jsonl per coin -> {ts, close}.

    python scripts/research/icebreaker_fetch_klines.py \
        --symbols 1000BONKUSDT ... --start 2026-03-01 --end 2026-04-01 --out data/klines
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STEP_MS = 5 * 60_000
WINDOW = 1000 * STEP_MS


def to_ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch(symbol, start, end, retries=4):
    out = {}
    t = start
    while t < end:
        e = min(t + WINDOW, end)
        url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
               f"&interval=5&start={t}&end={e}&limit=1000")
        for attempt in range(retries):
            try:
                r = json.load(urllib.request.urlopen(url, timeout=20))
                if r.get("retCode") != 0:
                    raise RuntimeError(r.get("retMsg"))
                for row in r.get("result", {}).get("list", []):
                    out[int(row[0])] = float(row[4])   # [start, o, h, l, close, ...]
                break
            except Exception as ex:
                if attempt == retries - 1:
                    print(f"  [{symbol}] window {t} FAILED: {ex}", flush=True)
                else:
                    time.sleep(1.0 + attempt)
        t = e
        time.sleep(0.15)
    return sorted(out.items())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    start, end = to_ms(args.start), to_ms(args.end)
    for sym in args.symbols:
        series = fetch(sym, start, end)
        with open(args.out / f"{sym}.jsonl", "w") as f:
            for ts, close in series:
                f.write(json.dumps({"ts": ts, "close": close}) + "\n")
        print(f"[{sym}] {len(series)} bars", flush=True)


if __name__ == "__main__":
    main()
