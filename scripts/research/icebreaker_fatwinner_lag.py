"""Final-round task (1)+(2) bridge: the momentum napkin (icebreaker_fatwinner_vel.py)
found pre-break momentum separates fat winners (mom15 AUC 0.74). But that window ENDS at
the breakout bar -> it partly measures the break spike itself (entrable only at the spike
top = bad price, Phase 1.9 wall). Decisive question: does the separation survive when the
momentum window ENDS k bars BEFORE the breakout bar? If yes -> a genuine pre-break signal
you could read while the level is only being approached (arm + enter near level). If the
AUC collapses to 0.5 as we step back -> it's the break candle, same spike wall.

mom over a 30m (6×5m) window ENDING at bar (idx - k), signed by break side, for k=0..6.
"Fat" = top-decile MFE within (symbol, month) cell.

    python scripts/research/icebreaker_fatwinner_lag.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DUMPS = [
    ("MAR", ROOT / "data" / "ib_major_liq.jsonl", ROOT / "data" / "klines"),
    ("APR", ROOT / "data" / "ib_major_apr_liq.jsonl", ROOT / "data" / "klines_apr"),
    ("MAY", ROOT / "data" / "ib_major_may_liq.jsonl", ROOT / "data" / "klines_may"),
]
TOP_Q = 0.10
WIN = 6          # 30m momentum window
LAGS = [0, 1, 2, 3, 4, 6]


def load_closes(path):
    ts, cl = [], []
    for line in open(path):
        if line.strip():
            r = json.loads(line); ts.append(r["ts"]); cl.append(r["close"])
    return np.array(ts), np.array(cl)


def auc(pos, neg):
    pos = [v for v in pos if v == v]; neg = [v for v in neg if v == v]
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks = {}; i = 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[k] = r
        i = j
    rs = sum(ranks[idx] for idx, (_, lab) in enumerate(allv) if lab == 1)
    n1 = len(pos)
    return (rs - n1 * (n1 + 1) / 2.0) / (n1 * len(neg))


def main():
    rows = []
    for tag, dump, kdir in DUMPS:
        recs = [json.loads(l) for l in open(dump) if l.strip()]
        for r in recs:
            r["month"] = tag
        by_sym = defaultdict(list)
        for r in recs:
            by_sym[r["symbol"]].append(r)
        for sym, srecs in by_sym.items():
            kpath = kdir / f"{sym}.jsonl"
            if not kpath.exists():
                continue
            ts, cl = load_closes(kpath)
            for r in srecs:
                idx = int(np.searchsorted(ts, r["ts_close"], side="right") - 1)
                if idx - max(LAGS) - WIN < 0 or idx >= len(cl):
                    continue
                sign = 1.0 if r["side"] == "long" else -1.0
                for k in LAGS:
                    e = idx - k
                    r[f"mom_k{k}"] = sign * (cl[e] / cl[e - WIN] - 1)
                rows.append(r)
    print(f"joined {len(rows)} setups\n")

    cells = defaultdict(list)
    for r in rows:
        cells[(r["symbol"], r["month"])].append(r)
    for cr in cells.values():
        cr.sort(key=lambda r: r["mfe"], reverse=True)
        nf = max(1, int(round(len(cr) * TOP_Q)))
        for i, r in enumerate(cr):
            r["fat"] = 1 if i < nf else 0
    fat = [r for r in rows if r["fat"]]; rest = [r for r in rows if not r["fat"]]
    print(f"fat = {len(fat)}  rest = {len(rest)}")
    print("  mom window ENDS at (idx - k);  k=0 includes break bar, k>0 = bars before it\n")
    print(f"  {'lag_k':>6s} {'mins_before':>11s} {'AUC_pool':>9s} {'MAR':>6s} {'APR':>6s} {'MAY':>6s}")
    for k in LAGS:
        f = f"mom_k{k}"
        ap = auc([r.get(f) for r in fat], [r.get(f) for r in rest])
        per = [auc([r.get(f) for r in fat if r["month"] == t],
                   [r.get(f) for r in rest if r["month"] == t]) for t, *_ in DUMPS]
        print(f"  {k:6d} {k*5:11d} {ap:9.3f} {per[0]:6.2f} {per[1]:6.2f} {per[2]:6.2f}")


if __name__ == "__main__":
    main()
