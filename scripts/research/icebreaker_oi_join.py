"""Join OI to gated setups: does rising OI into the break predict the move?

For each setup, dOI% = (OI at break - OI window_min before) / OI before. Hypothesis:
rising OI = new positions fueling the breakout -> higher good-rate (MFE>=good) and
better realized PnL. Reports good-rate split (rising vs falling), AUC of dOI, per-coin
robustness, and dOI-quartile lift. Dumps OI-tagged setups for a downstream exit-sweep
on the OI-confirmed subset.

    python scripts/research/icebreaker_oi_join.py \
        --cache 'data/ib_gated_*.jsonl' --oi data/oi --window-min 30 \
        --good-mfe 0.01 --dump data/ib_gated_oi.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

import numpy as np


def load_oi(oi_dir):
    series = {}
    for fp in glob.glob(str(Path(oi_dir) / "*.jsonl")):
        sym = Path(fp).stem
        rows = [json.loads(l) for l in open(fp) if l.strip()]
        rows.sort(key=lambda r: r["ts"])
        series[sym] = (np.array([r["ts"] for r in rows]),
                       np.array([r["oi"] for r in rows]))
    return series


def oi_at(ts_arr, oi_arr, t):
    """Most recent OI value at or before t (causal). None if before coverage."""
    i = bisect_right(ts_arr, t) - 1
    return float(oi_arr[i]) if i >= 0 else None


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks = {}
    i = 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[k] = r
        i = j
    rp = sum(ranks[idx] for idx, (_, lab) in enumerate(allv) if lab == 1)
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def gr(rows, good_mfe):
    return (sum(1 for r in rows if r["mfe"] >= good_mfe) / len(rows)) if rows else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", required=True)
    p.add_argument("--oi", required=True)
    p.add_argument("--window-min", type=int, default=30)
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--dump", type=Path, default=None)
    args = p.parse_args()

    recs = []
    for fp in sorted(glob.glob(args.cache)):
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    oi = load_oi(args.oi)
    win_ms = args.window_min * 60_000

    tagged, missing = [], 0
    for r in recs:
        sym = r["symbol"]
        if sym not in oi:
            missing += 1
            continue
        ts_arr, oi_arr = oi[sym]
        now = oi_at(ts_arr, oi_arr, r["ts_close"])
        prev = oi_at(ts_arr, oi_arr, r["ts_close"] - win_ms)
        if now is None or prev is None or prev <= 0:
            missing += 1
            continue
        r = {**r, "doi_pct": (now - prev) / prev}
        tagged.append(r)

    n = len(tagged)
    base = gr(tagged, args.good_mfe)
    rising = [r for r in tagged if r["doi_pct"] > 0]
    falling = [r for r in tagged if r["doi_pct"] <= 0]

    print("=" * 78)
    print(f"  OI JOIN  window={args.window_min}m  n={n} (missing OI {missing})  "
          f"base good(MFE>={args.good_mfe:.0%})={base:.1%}")
    print("=" * 78)
    print(f"  rising OI   n={len(rising):4d}  good={gr(rising, args.good_mfe):.1%}")
    print(f"  falling OI  n={len(falling):4d}  good={gr(falling, args.good_mfe):.1%}")
    grr, grf = gr(rising, args.good_mfe), gr(falling, args.good_mfe)
    if grf and grf == grf:
        print(f"  LIFT rising/falling = {grr / grf:.2f}x")
    # AUC of dOI separating good vs bad
    good = [r["doi_pct"] for r in tagged if r["mfe"] >= args.good_mfe]
    bad = [r["doi_pct"] for r in tagged if r["mfe"] < args.good_mfe]
    print(f"  AUC(dOI -> good) = {auc(good, bad):.3f}   "
          f"(>0.5 = higher dOI predicts the move)")

    # quartiles by dOI
    vals = sorted(r["doi_pct"] for r in tagged)
    qs = [vals[int(n * q)] for q in (0.25, 0.5, 0.75)]
    print(f"  dOI quartiles: q25={qs[0]:+.2%} q50={qs[1]:+.2%} q75={qs[2]:+.2%}")
    for lo, hi, name in [(-1e9, qs[0], "Q1 (most negative)"), (qs[0], qs[1], "Q2"),
                         (qs[1], qs[2], "Q3"), (qs[2], 1e9, "Q4 (most positive)")]:
        b = [r for r in tagged if lo <= r["doi_pct"] < hi]
        print(f"    {name:18s} n={len(b):4d}  good={gr(b, args.good_mfe):.1%}")

    print("\n  per-coin good-rate  rising / falling:")
    for sym in sorted(set(r["symbol"] for r in tagged)):
        rs = [r for r in tagged if r["symbol"] == sym and r["doi_pct"] > 0]
        fs = [r for r in tagged if r["symbol"] == sym and r["doi_pct"] <= 0]
        print(f"    {sym:14s} rising n={len(rs):3d} good={gr(rs, args.good_mfe):5.1%}   "
              f"falling n={len(fs):3d} good={gr(fs, args.good_mfe):5.1%}")

    if args.dump:
        with open(args.dump, "w") as f:
            for r in tagged:
                f.write(json.dumps(r) + "\n")
        print(f"\n  dumped {n} OI-tagged setups -> {args.dump}")


if __name__ == "__main__":
    main()
