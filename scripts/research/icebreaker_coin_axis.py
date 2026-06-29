"""What separates coins where CLEAN x mom WORKS (PEPE/BONK/FART) from where it doesn't
(POPCAT/WLD)? vol-Pearson was ~0, so the axis is something else. Per coin: realized edge
(CLEAN top-q net, operating rule) vs candidate ex-ante features (daily $turnover, realized
vol, price level, setup frequency, median MFE). Pearson + a table sorted by edge.

klines exit is ~uniformly optimistic, so per-coin RELATIVE ordering (the axis) is reliable
even if absolute net is an upper bound.

    python scripts/research/icebreaker_coin_axis.py \
        --dump data/ib_momdump_wide.jsonl --klines data/klines_wide --topq 0.15
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else float("nan")


def rvol(closes):
    cl = [c for c in closes if c > 0]
    if len(cl) < 30:
        return None
    r = [math.log(cl[k] / cl[k - 1]) for k in range(1, len(cl)) if cl[k - 1] > 0]
    m = sum(r) / len(r)
    v = sum((x - m) ** 2 for x in r) / len(r)
    return math.sqrt(v) * math.sqrt(1440) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--klines", required=True)
    ap.add_argument("--topq", type=float, default=0.15)
    ap.add_argument("--min-sel", type=int, default=15, help="min selected setups/coin")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    kdir = Path(args.klines)
    syms = sorted(set(r["sym"] for r in rows))

    # per-coin features from klines
    feat = {}
    for sym in syms:
        p = kdir / f"{sym}.jsonl"
        if not p.exists():
            continue
        closes, turn = [], []
        for line in open(p):
            if line.strip():
                b = json.loads(line)
                closes.append(b["close"])
                turn.append(b["close"] * b.get("volume", 0.0))  # quote turnover/bar
        if len(closes) < 1000:
            continue
        v = rvol(closes)
        days = len(closes) / 1440.0
        feat[sym] = {
            "turn_usd_d": sum(turn) / days,                      # avg daily $ turnover
            "vol": v,
            "price": sorted(closes)[len(closes) // 2],
            "nbars": len(closes),
        }

    # per-coin edge: CLEAN top-q by mom within each month, pooled
    by_cm = defaultdict(list)
    for r in rows:
        if r["clean"]:
            by_cm[(r["sym"], r["month"])].append(r)
    sel_by_coin = defaultdict(list)
    for (sym, mo), rs in by_cm.items():
        rs = sorted(rs, key=lambda r: r["mom"], reverse=True)
        k = max(1, int(round(len(rs) * args.topq)))
        sel_by_coin[sym].extend(rs[:k])

    recs = []
    for sym in syms:
        sel = sel_by_coin.get(sym, [])
        if sym not in feat or len(sel) < args.min_sel:
            continue
        nets = [r["net_trail"] for r in sel]
        edge = sum(nets) / len(nets)
        wr = sum(1 for x in nets if x > 0) / len(nets)
        mfe_med = sorted(r["mfe"] for r in sel)[len(sel) // 2]
        f = feat[sym]
        recs.append({"sym": sym, "edge": edge, "wr": wr, "n": len(sel),
                     "mfe_med": mfe_med, **f})

    recs.sort(key=lambda r: r["edge"], reverse=True)
    print(f"coins={len(recs)}  edge=CLEAN top-{args.topq:.0%} net (klines, upper bound)\n")
    print(f"  {'coin':<14} {'edge%':>7} {'WR':>4} {'n':>4} {'turn$/d':>10} {'vol':>6} "
          f"{'price':>10} {'mfeMed%':>7}")
    for r in recs:
        print(f"  {r['sym']:<14} {r['edge']*100:+7.3f} {r['wr']*100:3.0f}% {r['n']:>4} "
              f"{r['turn_usd_d']/1e6:9.1f}M {r['vol']:6.1f} {r['price']:10.4g} {r['mfe_med']*100:7.2f}")

    # correlations of edge vs candidate axes
    print("\n  Pearson(edge, feature) across coins:")
    edges = [r["edge"] for r in recs]
    for name, key, tf in (("log10 turnover", "turn_usd_d", lambda x: math.log10(max(x, 1))),
                          ("realized vol", "vol", lambda x: x),
                          ("log10 price", "price", lambda x: math.log10(max(x, 1e-9))),
                          ("median MFE", "mfe_med", lambda x: x),
                          ("setup count", "n", lambda x: x)):
        xs = [tf(r[key]) for r in recs]
        print(f"    {name:<16} {pearson(xs, edges):+.3f}")


if __name__ == "__main__":
    main()
