"""Liquidation-cluster map from OI+price, tested on gated breakouts.

Liq clusters are NOT order-book walls: they are projected FORCED market orders that
ACCELERATE a move (a long at L x liquidates ~ entry*(1-1/L) below; that forced sell
fuels a downside break). Invisible in the book, so we estimate them from aggregate OI:

  causal map (per coin, 5min):
    dOI>0 at price P -> new positions opened ~P. Split 50/50 long/short, spread over a
      FIXED leverage grid; project liq prices (long below, short above) into a price
      histogram of pending liq notional.
    dOI<0           -> scale all pending down (positions closed).
    each step       -> remove cohorts whose liq price the market has now reached
                       (they liquidated) -> map holds only still-pending fuel.

  feature per breakout: liq_ahead = pending SAME-DIRECTION liq notional in the band
    just beyond the level (short break -> long liqs below; long break -> short liqs
    above), normalized by current OI notional. Hypothesis: breaking TOWARD a dense
    nearby liq cluster -> bigger/▸more directional move.

Assumptions are FIXED (not tuned): leverage grid + maintenance margin + 50/50 split.
If signal only appears after tuning these, it isn't real.

    python scripts/research/icebreaker_liqmap.py --cache 'data/ib_gated_*.jsonl' \
        --oi data/oi --klines data/klines --band 0.05 --dump data/ib_liqmap.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

# FIXED model assumptions (documented, not optimized)
LEV = [(10, 0.30), (25, 0.30), (50, 0.25), (100, 0.15)]   # leverage -> weight
MM = 0.005          # maintenance margin
LONG_FRAC = 0.5     # split of new OI (no funding tilt in the base model)
NB = 1200           # price-grid buckets per coin


def load_series(oi_dir, kl_dir):
    """Per coin: ts-aligned arrays (ts, oi, price) over the OI∩kline timestamps."""
    out = {}
    for fp in glob.glob(str(Path(oi_dir) / "*.jsonl")):
        sym = Path(fp).stem
        oi = {int(r["ts"]): float(r["oi"]) for r in (json.loads(l) for l in open(fp) if l.strip())}
        kp = Path(kl_dir) / f"{sym}.jsonl"
        if not kp.exists():
            continue
        px = {int(r["ts"]): float(r["close"]) for r in (json.loads(l) for l in open(kp) if l.strip())}
        ts = sorted(set(oi) & set(px))
        if len(ts) < 50:
            continue
        out[sym] = (np.array(ts), np.array([oi[t] for t in ts]),
                    np.array([px[t] for t in ts]))
    return out


def run_coin(ts, oi, price, setups, band):
    """Walk the 5min series building the causal liq map; emit liq_ahead per setup.

    setups: list of dicts with ts_close, level, side (already this coin). Returns the
    same dicts augmented with liq_ahead, liq_ahead_rel, oi_notional."""
    lo = float(price.min()) * 0.80
    hi = float(price.max()) * 1.20
    edges = np.linspace(lo, hi, NB + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    p_long = np.zeros(NB)      # pending long-liq notional by liq price (below entry)
    p_short = np.zeros(NB)     # pending short-liq notional by liq price (above entry)

    # setups sorted by ts; emit when the walk passes each
    sl = sorted(setups, key=lambda s: s["ts_close"])
    si = 0
    out = []
    prev_oi = oi[0]
    for k in range(len(ts)):
        P = price[k]
        O = oi[k]
        # 1) liquidation cleanup: market reached liq prices -> those cohorts gone
        p_long[centers >= P] = 0.0      # long liq is below entry; hit when price falls to it
        p_short[centers <= P] = 0.0     # short liq is above entry; hit when price rises to it
        # 2) OI change
        if O > prev_oi + 1e-9:
            add = (O - prev_oi) * P     # new notional opened ~P
            for L, w in LEV:
                lp = P * (1 - 1 / L + MM)
                sp = P * (1 + 1 / L - MM)
                bl = min(max(int((lp - lo) / (hi - lo) * NB), 0), NB - 1)
                bs = min(max(int((sp - lo) / (hi - lo) * NB), 0), NB - 1)
                p_long[bl] += add * LONG_FRAC * w
                p_short[bs] += add * (1 - LONG_FRAC) * w
        elif O < prev_oi - 1e-9 and prev_oi > 0:
            r = O / prev_oi
            p_long *= r; p_short *= r
        prev_oi = O

        # 3) emit features for setups whose close falls at/just after this 5min point
        nxt = ts[k + 1] if k + 1 < len(ts) else ts[k] + 5 * 60_000
        while si < len(sl) and sl[si]["ts_close"] < nxt:
            s = sl[si]; si += 1
            L = s["level"]
            oi_notional = O * P
            if s["side"] == "short":     # break down: fuel = long liqs just BELOW level
                sel = (centers >= L * (1 - band)) & (centers < L)
                ahead = float(p_long[sel].sum())
            else:                        # break up: fuel = short liqs just ABOVE level
                sel = (centers > L) & (centers <= L * (1 + band))
                ahead = float(p_short[sel].sum())
            out.append({**s, "liq_ahead": ahead,
                        "liq_ahead_rel": (ahead / oi_notional) if oi_notional > 0 else 0.0,
                        "oi_notional": oi_notional})
    # any setups past the series end: no map -> skip
    return out


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg]); ranks = {}; i = 0
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


def spearman(xs, ys):
    def rank(a):
        order = sorted(range(len(a)), key=lambda i: a[i]); r = [0.0] * len(a); i = 0
        while i < len(a):
            j = i
            while j < len(a) and a[order[j]] == a[order[i]]:
                j += 1
            avg = (i + j - 1) / 2.0
            for k in range(i, j):
                r[order[k]] = avg
            i = j
        return r
    rx, ry = rank(xs), rank(ys); m = len(xs); mx = sum(rx) / m; my = sum(ry) / m
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(m))
    dx = math.sqrt(sum((v - mx) ** 2 for v in rx)); dy = math.sqrt(sum((v - my) ** 2 for v in ry))
    return num / (dx * dy) if dx * dy else float("nan")


def gr(rows, gm):
    return sum(1 for r in rows if r["mfe"] >= gm) / len(rows) if rows else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", required=True)
    p.add_argument("--oi", required=True)
    p.add_argument("--klines", required=True)
    p.add_argument("--band", type=float, default=0.05, help="liq band just beyond level")
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--dump", type=Path, default=None)
    args = p.parse_args()

    recs = []
    for fp in sorted(glob.glob(args.cache)):
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    by_sym = defaultdict(list)
    for r in recs:
        by_sym[r["symbol"]].append(r)
    series = load_series(args.oi, args.klines)

    tagged = []
    for sym, sets in by_sym.items():
        if sym not in series:
            print(f"[{sym}] no OI/kline series", flush=True)
            continue
        ts, oi, price = series[sym]
        tagged.extend(run_coin(ts, oi, price, sets, args.band))

    n = len(tagged); base = gr(tagged, args.good_mfe)
    good = [r for r in tagged if r["mfe"] >= args.good_mfe]
    bad = [r for r in tagged if r["mfe"] < args.good_mfe]
    f = "liq_ahead_rel"
    print("=" * 72)
    print(f"  LIQ-MAP  n={n}  base good={base:.1%}  band={args.band:.0%}  "
          f"(lev {[l for l,_ in LEV]}, split L{LONG_FRAC:.0%})")
    print("=" * 72)
    print(f"  {f}: AUC={auc([r[f] for r in good],[r[f] for r in bad]):.3f}  "
          f"corr_MFE={spearman([r[f] for r in tagged],[r['mfe'] for r in tagged]):+.3f}")
    vals = sorted(r[f] for r in tagged); q = [vals[int(n * x)] for x in (.25, .5, .75)]
    print(f"  quartiles by {f} (good-rate):")
    for lo_, hi_, nm in [(-1, q[0], "Q1 (no/low fuel)"), (q[0], q[1], "Q2"),
                         (q[1], q[2], "Q3"), (q[2], 1e18, "Q4 (most fuel ahead)")]:
        b = [r for r in tagged if lo_ <= r[f] < hi_]
        if b:
            print(f"    {nm:22s} n={len(b):4d}  good={gr(b, args.good_mfe):5.1%}  "
                  f"mfe_med={sorted(x['mfe'] for x in b)[len(b)//2]:.2%}")
    print("\n  per-coin AUC (robustness — must hold beyond one coin):")
    for sym in sorted(set(r["symbol"] for r in tagged)):
        s = [r for r in tagged if r["symbol"] == sym]
        g = [r[f] for r in s if r["mfe"] >= args.good_mfe]
        bd = [r[f] for r in s if r["mfe"] < args.good_mfe]
        print(f"    {sym:14s} n={len(s):4d} base={gr(s,args.good_mfe):5.1%} "
              f"AUC={auc(g,bd):.2f}")
    if args.dump:
        with open(args.dump, "w") as fo:
            for r in tagged:
                fo.write(json.dumps(r) + "\n")
        print(f"\n  dumped {n} -> {args.dump}")


if __name__ == "__main__":
    main()
