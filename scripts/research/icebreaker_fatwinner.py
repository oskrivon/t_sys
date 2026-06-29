"""Final-round task (1) NAPKIN: does the EXTREME MFE tail (fat winners — the trades a
discretionary scalper actually takes) separate from the rest on the features we already
have, *before* spending server time on the wide-spectrum (kalman/pre-break velocity/
break-volume) run?

Prior (Phase 2 Angle 3): multivariate logreg+GBT over 22 features gave CV-AUC 0.49-0.51
for good@1% AND win. But that was on the good@1% threshold, NOT the extreme tail. This
napkin tests the tail on the ready major-dump features.

Confound control (Phase 3 vol-insight): MFE follows coin/month volatility, so "fat" is
labelled WITHIN each (symbol, month) cell — top-q by MFE — to neutralize the vol/coin
identity confound. A feature that only AUC>0.5 pooled but ~0.5 within-cell is just vol.

Features in dump: touches, span_bars, one_sided, liq_ahead_rel, oi_notional, mae,
t_peak_ms, side; derived: spacing = span_bars/(touches-1), hour, dow.

    python scripts/research/icebreaker_fatwinner.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DUMPS = [
    ("MAR", ROOT / "data" / "ib_major_liq.jsonl"),
    ("APR", ROOT / "data" / "ib_major_apr_liq.jsonl"),
    ("MAY", ROOT / "data" / "ib_major_may_liq.jsonl"),
]
TOP_Q = 0.10          # fat = top decile of MFE within (symbol, month)
FEATURES = ["touches", "span_bars", "one_sided", "spacing", "liq_ahead_rel",
            "oi_notional", "mae", "abs_tpeak", "hour", "dow"]


def auc(pos, neg):
    """Mann-Whitney AUC: P(feature(fat) > feature(rest)). 0.5 = no separation."""
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
        r = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[k] = r
        i = j
    rank_sum = sum(ranks[idx] for idx, (_, lab) in enumerate(allv) if lab == 1)
    n1 = len(pos)
    return (rank_sum - n1 * (n1 + 1) / 2.0) / (n1 * len(neg))


def enrich(r):
    t = r.get("touches", 0)
    r["spacing"] = r["span_bars"] / (t - 1) if t and t > 1 else 0.0
    r["abs_tpeak"] = abs(r.get("t_peak_ms", 0))
    dt = datetime.fromtimestamp(r["ts_close"] / 1000, tz=timezone.utc)
    r["hour"] = dt.hour
    r["dow"] = dt.weekday()
    return r


def main():
    rows = []
    for tag, path in DUMPS:
        if not path.exists():
            print(f"MISSING {path}")
            continue
        for line in open(path):
            if not line.strip():
                continue
            r = enrich(json.loads(line))
            r["month"] = tag
            rows.append(r)
    print(f"loaded {len(rows)} setups across {len({r['month'] for r in rows})} months")

    # label fat WITHIN each (symbol, month) cell by MFE top-quantile
    cells = defaultdict(list)
    for r in rows:
        cells[(r["symbol"], r["month"])].append(r)
    for cell_rows in cells.values():
        cell_rows.sort(key=lambda r: r["mfe"], reverse=True)
        n_fat = max(1, int(round(len(cell_rows) * TOP_Q)))
        for i, r in enumerate(cell_rows):
            r["fat"] = 1 if i < n_fat else 0

    fat = [r for r in rows if r["fat"]]
    rest = [r for r in rows if not r["fat"]]
    print(f"fat (top {TOP_Q:.0%} within cell) = {len(fat)}  rest = {len(rest)}")
    print(f"fat MFE median = {sorted(r['mfe'] for r in fat)[len(fat)//2]:.4f}  "
          f"rest MFE median = {sorted(r['mfe'] for r in rest)[len(rest)//2]:.4f}\n")

    # pooled AUC per feature + per-month robustness
    print(f"  {'feature':>14s} {'AUC_pool':>9s} {'MAR':>6s} {'APR':>6s} {'MAY':>6s}  "
          f"{'fat_med':>10s} {'rest_med':>10s}")
    for f in FEATURES:
        a_pool = auc([r[f] for r in fat], [r[f] for r in rest])
        per = []
        for tag, _ in DUMPS:
            mf = [r[f] for r in fat if r["month"] == tag]
            mr = [r[f] for r in rest if r["month"] == tag]
            per.append(auc(mf, mr))
        fmed = sorted(r[f] for r in fat)[len(fat) // 2]
        rmed = sorted(r[f] for r in rest)[len(rest) // 2]
        print(f"  {f:>14s} {a_pool:9.3f} {per[0]:6.2f} {per[1]:6.2f} {per[2]:6.2f}  "
              f"{fmed:10.4g} {rmed:10.4g}")

    # side / direction split
    print()
    for side in ("long", "short"):
        fs = sum(1 for r in fat if r["side"] == side)
        rs = sum(1 for r in rest if r["side"] == side)
        tot = fs + rs
        print(f"  side={side:>5s}: fat-rate {fs/tot if tot else 0:.1%} "
              f"(fat {fs} / total {tot})")


if __name__ == "__main__":
    main()
