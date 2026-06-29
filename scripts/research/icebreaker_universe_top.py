"""Top-N Bybit linear perps by 24h turnover -> universe for the wide-universe klines test.
Prints a space-separated symbol list (USDT perps only, excludes obvious stables).

    python scripts/research/icebreaker_universe_top.py --n 50
"""
from __future__ import annotations

import argparse
import json
import urllib.request

EXCLUDE = {"USDCUSDT", "USDEUSDT", "EURUSDT", "BUSDUSDT", "DAIUSDT", "TUSDUSDT"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=50)
    args = p.parse_args()
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    rows = data.get("result", {}).get("list", [])
    perps = []
    for t in rows:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or s in EXCLUDE:
            continue
        try:
            to = float(t.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        perps.append((s, to))
    perps.sort(key=lambda x: x[1], reverse=True)
    top = perps[:args.n]
    print(f"# top {len(top)} of {len(perps)} USDT perps by 24h turnover")
    for s, to in top:
        print(f"#   {s:16s} ${to/1e6:,.0f}M")
    print(" ".join(s for s, _ in top))


if __name__ == "__main__":
    main()
