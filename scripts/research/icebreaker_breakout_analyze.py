"""Characterize GOOD breakouts (big MFE) vs fakeouts from the study dump.

For each pre-trade feature, reports how well it separates movers (MFE>=thr) from
the rest: median in each group, AUC (rank-sum; 0.5=useless, >0.5 higher feature
-> more likely good, <0.5 inverse), and the good-rate lift in the feature's top
quartile vs the base rate. No sklearn — univariate first; ML only if a feature
actually separates (small-n overfits otherwise).

    python scripts/research/icebreaker_breakout_analyze.py \
        --dump data/ib_breakouts.jsonl --good-mfe 0.01
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# pre-trade features known at/just after the break (valid predictors)
STRUCT = ["width", "touches", "squeeze", "vol_burst", "brk_strength"]
BOOK = ["wall_notional", "through_depth", "imbalance", "spread_bps"]


def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def auc(pos, neg):
    """Probability a random positive ranks above a random negative (rank-sum)."""
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    # average ranks for ties
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
    rank_pos = sum(ranks[idx] for idx, (_, lab) in enumerate(allv) if lab == 1)
    n_pos, n_neg = len(pos), len(neg)
    return (rank_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dump", type=Path, required=True)
    p.add_argument("--good-mfe", type=float, default=0.01)
    args = p.parse_args()

    recs = load(args.dump)
    n = len(recs)
    for r in recs:
        r["good"] = int(r["mfe"] >= args.good_mfe)
    good = [r for r in recs if r["good"]]
    bad = [r for r in recs if not r["good"]]
    base = len(good) / n if n else 0
    has_book = all(f in recs[0] for f in BOOK) if recs else False
    feats = STRUCT + (BOOK if has_book else [])

    print("=" * 84)
    print(f"  BREAKOUT CHARACTERIZATION  n={n}  good(MFE>={args.good_mfe:.1%})="
          f"{len(good)} ({base:.1%})  book={'yes' if has_book else 'no'}")
    print("=" * 84)
    mfes = sorted(r["mfe"] for r in recs)
    maes = sorted(r["mae"] for r in recs)
    print(f"  MFE median={med(mfes):.2%} p90={mfes[int(n*.9)]:.2%}  "
          f"MAE median={med(maes):.2%}")
    print(f"  {'feature':14s} {'AUC':>6s}  {'good_med':>10s} {'bad_med':>10s}  "
          f"{'topQ good-rate (lift)':>22s}")
    rows = []
    for f in feats:
        gv = [r[f] for r in good if f in r]
        bv = [r[f] for r in bad if f in r]
        a = auc(gv, bv)
        # top-quartile lift
        allv = sorted((r[f] for r in recs if f in r))
        if allv:
            q75 = allv[int(len(allv) * 0.75)]
            top = [r for r in recs if r.get(f, q75 - 1) >= q75]
            tr = (sum(x["good"] for x in top) / len(top)) if top else float("nan")
        else:
            tr = float("nan")
        rows.append((abs(a - 0.5), f, a, med(gv), med(bv), tr))
    for _, f, a, gm, bm, tr in sorted(rows, reverse=True):
        lift = tr / base if base else float("nan")
        print(f"  {f:14s} {a:6.2f}  {gm:10.4f} {bm:10.4f}  "
              f"{tr:6.1%}  (x{lift:.2f})")

    # per-coin good rate (is it concentrated?)
    print("\n  per-coin good-rate:")
    for sym in sorted(set(r["symbol"] for r in recs)):
        s = [r for r in recs if r["symbol"] == sym]
        print(f"    {sym:10s} n={len(s):4d}  good={sum(x['good'] for x in s)/len(s):5.1%}")

    print("\n  AUC reading: >0.60 or <0.40 = some signal; ~0.50 = useless.")


if __name__ == "__main__":
    main()
