"""Does order-flow at the touch separate real breaks from fakeouts? (Phase 2b / TIB)

Reads the per-cross dump from icebreaker_levelcross.py (flow features computed strictly
BEFORE the cross + the realized outcome). For each flow feature reports AUC vs
runner(MFE>=1%) and vs win(net>0); then sweeps a threshold gate and reports the kept
subset's expectancy / WR / payoff / retained fraction — i.e. can a real-time flow gate
turn the -0.08% armed-cross into a positive, fillable edge by cutting the 81% fakeouts?

    python scripts/research/icebreaker_flow_gate.py --dumps '/root/trading/tmp/ib_lc_*.jsonl'
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict

ID_COLS = {"symbol", "side", "level", "ts_cross", "gross", "fee_units", "reason_exit",
           "mfe", "net"}


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


def quantile(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def gate_stats(rows, fee):
    n = len(rows)
    if not n:
        return (0, float("nan"), float("nan"), float("nan"))
    nets = [r["net"] for r in rows]
    wins = [x for x in nets if x > 0]; losses = [x for x in nets if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    return (n, sum(nets) / n, len(wins) / n, abs(aw / al) if al else 0.0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dumps", required=True)
    p.add_argument("--fee", type=float, default=0.00055)
    p.add_argument("--gate", default="", help="feature to sweep a threshold on (default: best AUC vs win)")
    args = p.parse_args()

    rows = []
    for fp in sorted(glob.glob(args.dumps)):
        rows.extend(json.loads(l) for l in open(fp) if l.strip())
    n = len(rows)
    feats = [k for k in rows[0] if k not in ID_COLS and isinstance(rows[0][k], (int, float))]
    base_good = sum(1 for r in rows if r["mfe"] >= 0.01) / n
    base_win = sum(1 for r in rows if r["net"] > 0) / n
    print("=" * 88)
    print(f"  FLOW GATE  n={n}  base good(MFE>=1%)={base_good:.1%}  base win={base_win:.1%}  "
          f"mean_net={sum(r['net'] for r in rows)/n*100:+.3f}%")
    print("=" * 88)
    print(f"  {'feature':10s} {'AUC_good':>9s} {'AUC_win':>8s} {'mean@win':>9s} {'mean@loss':>10s}")
    ranked = []
    for f in feats:
        gp = [r[f] for r in rows if r["mfe"] >= 0.01]; gn = [r[f] for r in rows if r["mfe"] < 0.01]
        wp = [r[f] for r in rows if r["net"] > 0]; wn = [r[f] for r in rows if r["net"] <= 0]
        a_g, a_w = auc(gp, gn), auc(wp, wn)
        mw = sum(wp) / len(wp) if wp else 0; ml = sum(wn) / len(wn) if wn else 0
        ranked.append((a_w, f, a_g, mw, ml))
    for a_w, f, a_g, mw, ml in sorted(ranked, key=lambda x: -abs(x[0] - 0.5)):
        print(f"  {f:10s} {a_g:9.3f} {a_w:8.3f} {mw:9.3f} {ml:10.3f}")

    gate = args.gate or max(ranked, key=lambda x: abs(x[0] - 0.5))[1]
    print(f"\n  --- threshold-gate sweep on '{gate}' (keep feature >= thr) ---")
    print(f"  {'keep_top':>9s} {'thr':>8s} {'n':>5s} {'frac':>5s} {'mean_net%':>10s} {'WR':>5s} {'payoff':>7s}")
    vals = [r[gate] for r in rows]
    for q in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9):
        thr = quantile(vals, q)
        kept = [r for r in rows if r[gate] >= thr]
        nn, mean, wr, payoff = gate_stats(kept, args.fee)
        print(f"  top-{(1-q)*100:3.0f}%  {thr:8.3f} {nn:5d} {nn/n:5.0%} {mean*100:+10.3f} "
              f"{wr:5.0%} {payoff:7.2f}")


if __name__ == "__main__":
    main()
