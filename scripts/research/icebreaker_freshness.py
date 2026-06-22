"""Tag stale re-breaks: was the level ALREADY broken before our signal?

The detector's one_sided gate only checks the *fraction* of base closes on the
holding side, so a level the price has oscillated through (broke, pulled back, broke
again) can still pass. Those re-breaks are noise. Here, for each setup we rebuild
bars and count how many bars in the lookback BEFORE the signal already closed
decisively beyond the level in the break direction. prior_breaks==0 = a fresh first
break; >0 = the level was already worked. Reports good-rate fresh vs stale and dumps
the fresh subset for a downstream exit-sweep.

    python scripts/research/icebreaker_freshness.py \
        --store /root/trading/data/icebreaker_active --cache 'data/ib_gated_*.jsonl' \
        --good-mfe 0.01 --dump data/ib_gated_fresh.jsonl
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import sys
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


sbt = _load("icebreaker_signal_backtest", "scripts/research/icebreaker_signal_backtest.py")
mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")


def gr(rows, good_mfe):
    return (sum(1 for r in rows if r["mfe"] >= good_mfe) / len(rows)) if rows else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-31")
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--lookback", type=int, default=480)
    p.add_argument("--brk", type=float, default=0.0015)
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--dump", type=Path, default=None)
    p.add_argument("--dump-all", type=Path, default=None,
                   help="dump every setup tagged with prior_breaks (for combo filters)")
    args = p.parse_args()

    recs = []
    for fp in sorted(glob.glob(args.cache)):
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    by_sym = defaultdict(list)
    for r in recs:
        by_sym[r["symbol"]].append(r)

    store = Path(args.store)
    tagged = []
    for sym, srecs in by_sym.items():
        bars = []
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            t_ts, t_pr, t_qty, _ = mc.load_trades_arr(store, sym, date)
            if len(t_ts) == 0:
                continue
            bars.extend(mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms))
        if not bars:
            continue
        bts = np.array([b["ts"] for b in bars])
        c = np.array([b["close"] for b in bars])
        for r in srecs:
            i = int(np.searchsorted(bts, r["ts_close"] - args.bar_ms))
            if i <= 0 or i >= len(bars):
                continue
            L = r["level"]
            lo = max(0, i - args.lookback)
            win = c[lo:i]
            if r["side"] == "short":          # break DOWN: prior closes below level
                prior = int(np.sum(win < L * (1 - args.brk)))
            else:                              # break UP: prior closes above level
                prior = int(np.sum(win > L * (1 + args.brk)))
            tagged.append({**r, "prior_breaks": prior})

    n = len(tagged)
    fresh = [r for r in tagged if r["prior_breaks"] == 0]
    stale = [r for r in tagged if r["prior_breaks"] > 0]
    print("=" * 72)
    print(f"  FRESHNESS  n={n}  fresh(prior=0)={len(fresh)} ({len(fresh)/n:.0%})  "
          f"stale={len(stale)} ({len(stale)/n:.0%})")
    print("=" * 72)
    print(f"  base good(MFE>={args.good_mfe:.0%}) = {gr(tagged, args.good_mfe):.1%}")
    print(f"  FRESH  good = {gr(fresh, args.good_mfe):.1%}")
    print(f"  STALE  good = {gr(stale, args.good_mfe):.1%}")
    gf, gs = gr(fresh, args.good_mfe), gr(stale, args.good_mfe)
    if gs:
        print(f"  LIFT fresh/stale = {gf / gs:.2f}x")
    # buckets by prior_breaks count
    print("\n  by prior_breaks bucket:")
    for lo, hi, name in [(0, 1, "0 (fresh)"), (1, 6, "1-5"), (6, 21, "6-20"),
                         (21, 10**9, "21+")]:
        b = [r for r in tagged if lo <= r["prior_breaks"] < hi]
        if b:
            print(f"    {name:10s} n={len(b):4d}  good={gr(b, args.good_mfe):.1%}")
    print("\n  per-coin fresh-rate / fresh good-rate:")
    for sym in sorted(by_sym):
        s = [r for r in tagged if r["symbol"] == sym]
        f = [r for r in s if r["prior_breaks"] == 0]
        if s:
            print(f"    {sym:14s} fresh {len(f)}/{len(s)} ({len(f)/len(s):3.0%})  "
                  f"good={gr(f, args.good_mfe):5.1%}")

    if args.dump:
        with open(args.dump, "w") as fo:
            for r in fresh:
                fo.write(json.dumps(r) + "\n")
        print(f"\n  dumped {len(fresh)} fresh setups -> {args.dump}")
    if args.dump_all:
        with open(args.dump_all, "w") as fo:
            for r in tagged:
                fo.write(json.dumps(r) + "\n")
        print(f"  dumped {len(tagged)} tagged setups -> {args.dump_all}")


if __name__ == "__main__":
    main()
