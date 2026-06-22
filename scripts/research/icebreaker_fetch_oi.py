"""Fetch Bybit historical open-interest (5min) for the icebreaker coins.

OI is non-directional (every contract has a long and a short), so RISING OI during
a breakout = new positions being opened (fuel) rather than covering. Combined with
price direction this is the classic continuation tell. We pull a clean 5min OI
series so icebreaker_oi_join.py can compute dOI% over each setup's break window.

Bybit v5 /market/open-interest returns <=200 points newest-first; we walk the month
in forward windows. Output: one jsonl per coin -> {ts, oi}.

    python scripts/research/icebreaker_fetch_oi.py \
        --symbols 1000BONKUSDT 1000FLOKIUSDT 1000PEPEUSDT DOGEUSDT FARTCOINUSDT \
                  ORDIUSDT POPCATUSDT WIFUSDT WLDUSDT \
        --start 2026-03-01 --end 2026-04-01 --out data/oi
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STEP_MS = 5 * 60_000              # 5min interval
WINDOW = 200 * STEP_MS           # max points per call


def to_ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch(symbol, start, end, retries=4):
    """All (ts, oi) in [start, end), de-duped and ts-ascending."""
    out = {}
    t = start
    while t < end:
        e = min(t + WINDOW, end)
        url = (f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}"
               f"&intervalTime=5min&startTime={t}&endTime={e}&limit=200")
        for attempt in range(retries):
            try:
                r = json.load(urllib.request.urlopen(url, timeout=20))
                if r.get("retCode") != 0:
                    raise RuntimeError(r.get("retMsg"))
                for row in r.get("result", {}).get("list", []):
                    out[int(row["timestamp"])] = float(row["openInterest"])
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
        fp = args.out / f"{sym}.jsonl"
        with open(fp, "w") as f:
            for ts, oi in series:
                f.write(json.dumps({"ts": ts, "oi": oi}) + "\n")
        if series:
            span_h = (series[-1][0] - series[0][0]) / 3_600_000
            print(f"[{sym}] {len(series)} pts  span={span_h:.0f}h  -> {fp}", flush=True)
        else:
            print(f"[{sym}] EMPTY", flush=True)


if __name__ == "__main__":
    main()
