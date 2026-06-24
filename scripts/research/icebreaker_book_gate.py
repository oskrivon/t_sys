"""Does the L2 book at the touch separate real breaks from fakeouts? (Phase 2 Angle 2)

Flow/TIB at the cross was DEAD (AUC~0.50, icebreaker_flow_gate): the tape can't tell a
real break from a fakeout-poke, because BOTH start with aggression. The wall named in
Phase 2b: level-cross = good PRICE (payoff~2.2), bad SIGNAL (WR 19%, 81% are pokes). If a
real-time discriminator exists it's in RESTING liquidity (the book), not the tape.

For each armed cross (from icebreaker_levelcross.py dump, with ts_cross/side/level) we
reconstruct the resting book AT ts_cross and just BEFORE it (ts_cross - dt), all from
book events with ts <= the cut (no lookahead), and measure three families:

  void ahead     - resting notional on the RUN side within `band` ahead of the level.
                   Thin ahead -> price runs (Phase 1.7); thick -> absorbed (fakeout).
  support pull   - change in run/base resting depth & wall over the [ts_cross-dt, ts_cross]
                   window. Wall pulled away an instant before the touch = a maker stepping
                   aside for a real cascade; wall standing/refilling = absorption (fake).
  imbalance      - (base - run)/(base + run) resting depth near the level at the touch.

Then: AUC of every feature vs good(MFE>=1%) and win(net>0); and a threshold-gate sweep
(keep feature >= thr) reporting the kept subset's expectancy/WR/payoff/retained-fraction
-- i.e. can a book gate turn the -0.086% armed-cross into a positive, fillable edge by
cutting the 81% fakeouts? (March only: book store covers FART/WIF/PEPE for March.)

    python scripts/research/icebreaker_book_gate.py \
        --dump /root/trading/tmp/ib_lc_book3.jsonl \
        --store /root/trading/data/ib_book
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")
fg = _load("icebreaker_flow_gate", "scripts/research/icebreaker_flow_gate.py")
BID, ASK = 0, 1


def book_near(surv, L, t, tol, band):
    """Resting (depth_within_band, max_wall_at_level) per side as of time `t`.

    qty in book_diff is the ABSOLUTE resting size at a price (0 = removed). latest_per_price
    keeps the last size per price among events with ts<=t -> the live book at t. Zeros are
    dropped so a removed level doesn't count as resting liquidity."""
    s_ts, s_pr, s_qty, s_side = surv
    keep = s_ts <= t
    out = {}
    for bside in (BID, ASK):
        m = keep & (s_side == bside)
        uniq, last = mc.latest_per_price(s_pr[m], s_qty[m])
        nz = last > 0
        uniq, last = uniq[nz], last[nz]
        if len(uniq) == 0:
            out[bside] = (0.0, 0.0)
            continue
        notion = uniq * last
        sel = (uniq >= L * (1 - band)) & (uniq <= L * (1 + band))
        wall = float(notion[np.abs(uniq - L) <= L * tol].max(initial=0.0))
        out[bside] = (float(notion[sel].sum()), wall)
    return out


def book_features(surv, L, is_long, ts_cross, tol, band, dt_ms):
    """Book-derived real/fakeout features at the touch (all from ts <= ts_cross)."""
    run_s = ASK if is_long else BID      # price runs INTO the opposite book
    base_s = BID if is_long else ASK
    now = book_near(surv, L, ts_cross, tol, band)
    pre = book_near(surv, L, ts_cross - dt_ms, tol, band)
    run_d, run_w = now[run_s]
    base_d, base_w = now[base_s]
    prun_d, prun_w = pre[run_s]
    pbase_d, pbase_w = pre[base_s]
    tot = run_d + base_d
    eps = 1e-9
    return {
        # void ahead: lower run depth/wall = thinner ahead = more likely a real run
        "run_depth": run_d,
        "run_wall": run_w,
        "depth_ratio": run_d / (base_d + eps),
        "imbalance": (base_d - run_d) / (tot + eps),    # +: thin ahead / thick behind
        "total_depth": tot,
        # support/wall pull over [ts_cross-dt, ts_cross]: + = liquidity REMOVED before touch
        "run_wall_pull": (prun_w - run_w) / (prun_w + eps),
        "run_depth_pull": (prun_d - run_d) / (prun_d + eps),
        "base_pull": (pbase_d - base_d) / (pbase_d + eps),
        "base_wall_pull": (pbase_w - base_w) / (pbase_w + eps),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dump", required=True, help="armed-cross dump w/ ts_cross,side,level,symbol,net,mfe")
    p.add_argument("--store", required=True, help="L2 book store (data/ib_book)")
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--band", type=float, default=0.005)
    p.add_argument("--dt-ms", type=int, default=2000, help="pre-touch window for pull features")
    p.add_argument("--fee", type=float, default=0.00055)
    p.add_argument("--out", type=Path, default=None, help="optional: dump rows+book features")
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    if args.symbols:
        rows = [r for r in rows if r["symbol"] in args.symbols]
    rows = [r for r in rows if "ts_cross" in r and "level" in r and "side" in r]
    for r in rows:
        r["date"] = time.strftime("%Y-%m-%d", time.gmtime(r["ts_cross"] / 1000.0))
    by_day = defaultdict(list)
    for r in rows:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    feat_rows, no_book = [], 0
    for (sym, date), crosses in sorted(by_day.items()):
        levels = sorted(set(c["level"] for c in crosses))
        surv = mc.filter_book(store, sym, date, levels, args.band * 2)
        if len(surv[0]) == 0:
            no_book += len(crosses)
            continue
        for c in crosses:
            feat = book_features(surv, c["level"], c["side"] == "long", c["ts_cross"],
                                 args.tol, args.band, args.dt_ms)
            feat_rows.append({**c, **feat})
        print(f"[{sym} {date}] crosses={len(crosses)}", flush=True)

    n = len(feat_rows)
    if n == 0:
        print(f"no crosses with book ({no_book} had none) -- nothing to analyze")
        return
    FEATS = ["run_depth", "run_wall", "depth_ratio", "imbalance", "total_depth",
             "run_wall_pull", "run_depth_pull", "base_pull", "base_wall_pull"]
    base_good = sum(1 for r in feat_rows if r["mfe"] >= 0.01) / n
    base_win = sum(1 for r in feat_rows if r["net"] > 0) / n
    print("=" * 92)
    print(f"  BOOK GATE  n={n}  ({no_book} crosses had no book)  base good(MFE>=1%)={base_good:.1%}  "
          f"base win={base_win:.1%}  mean_net={sum(r['net'] for r in feat_rows)/n*100:+.3f}%")
    print("=" * 92)
    print(f"  {'feature':16s} {'AUC_good':>9s} {'AUC_win':>8s} {'mean@win':>11s} {'mean@loss':>11s}")
    ranked = []
    for f in FEATS:
        gp = [r[f] for r in feat_rows if r["mfe"] >= 0.01]; gn = [r[f] for r in feat_rows if r["mfe"] < 0.01]
        wp = [r[f] for r in feat_rows if r["net"] > 0]; wn = [r[f] for r in feat_rows if r["net"] <= 0]
        a_g, a_w = fg.auc(gp, gn), fg.auc(wp, wn)
        mw = sum(wp) / len(wp) if wp else 0.0; ml = sum(wn) / len(wn) if wn else 0.0
        ranked.append((a_w, f, a_g, mw, ml))
    for a_w, f, a_g, mw, ml in sorted(ranked, key=lambda x: -abs((x[0] if x[0] == x[0] else 0.5) - 0.5)):
        print(f"  {f:16s} {a_g:9.3f} {a_w:8.3f} {mw:11.4g} {ml:11.4g}")

    # gate on the strongest-separating feature, sweep both directions (keep >= and keep <=)
    best = max(ranked, key=lambda x: abs((x[0] if x[0] == x[0] else 0.5) - 0.5))[1]
    for direction in ("ge", "le"):
        print(f"\n  --- gate '{best}' keep {direction} thr ---")
        print(f"  {'cut':>6s} {'thr':>10s} {'n':>5s} {'frac':>5s} {'mean_net%':>10s} {'WR':>5s} {'payoff':>7s}")
        vals = [r[best] for r in feat_rows]
        for q in (0.0, 0.3, 0.5, 0.7, 0.8, 0.9):
            thr = fg.quantile(vals, q)
            kept = [r for r in feat_rows if (r[best] >= thr if direction == "ge" else r[best] <= thr)]
            nn, mean, wr, payoff = fg.gate_stats(kept, args.fee)
            print(f"  q{q:<4.2f} {thr:10.4g} {nn:5d} {nn/n:5.0%} {mean*100:+10.3f} {wr:5.0%} {payoff:7.2f}")

    if args.out:
        def _native(o):
            if isinstance(o, np.generic):
                return o.item()
            raise TypeError
        with open(args.out, "w") as fo:
            for r in feat_rows:
                fo.write(json.dumps(r, default=_native) + "\n")
        print(f"\n  dumped {n} rows+book features -> {args.out}")


if __name__ == "__main__":
    main()
