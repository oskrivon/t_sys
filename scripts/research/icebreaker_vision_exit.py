"""Exit mini-test under vision selection — does a runner-friendly exit convert the
vision-high setups into net-positive PnL?

The pilot tell: vision score 7-8 had POSITIVE median (+0.34%) but NEGATIVE mean — the
validated managed exit (TP1 50%@+0.6%->BE + tight 0.4% trail) caps the upside of the
higher-MFE setups vision picks while tail stops dominate the mean. So the bottleneck
may be the EXIT, not the selection. This re-walks the tape (NO new API calls) with
runner-friendly exit grids (ride / let-it-run) and asks, per exit config: is the
vision-TRADE / score>=7 subset net-positive at taker where ALL is breakeven?

Consumes the vision dump(s) (symbol/date/ts_close/side/level + vscore/vdecision) from
icebreaker_vision_select.py, so it pairs selection x exit on the same setups.

    python scripts/research/icebreaker_vision_exit.py \
        --store /root/trading/data/icebreaker_active \
        --vision '/root/trading/tmp/ib_vision_{mar_full,apr}.jsonl' --mode ride
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")
es = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")
sw = _load("icebreaker_exit_sweep", "scripts/research/icebreaker_exit_sweep.py")


SUBSETS = [
    ("ALL", lambda r: True),
    ("vision TRADE", lambda r: r["vdecision"] == "trade"),
    ("score>=6", lambda r: r["vscore"] >= 6),
    ("score>=7", lambda r: r["vscore"] >= 7),
]


def agg(rows, fee):
    """rows: list of (sym, gross, fee_units, risk). Returns (n, mean, med, win, sum, coins+/coins)."""
    if not rows:
        return (0, float("nan"), float("nan"), float("nan"), 0.0, 0, 0)
    nets = [g - fee * fu for _, g, fu, _ in rows]
    per = defaultdict(float)
    for (sym, g, fu, _), x in zip(rows, nets):
        per[sym] += x
    n = len(nets)
    return (n, sum(nets) / n, sorted(nets)[n // 2], sum(1 for x in nets if x > 0) / n,
            sum(nets), sum(1 for v in per.values() if v > 0), len(per))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--vision", required=True, help="glob of vision dump(s)")
    p.add_argument("--mode", choices=["managed", "ride", "letrun"], default="ride")
    p.add_argument("--fee-taker", type=float, default=0.00055)
    p.add_argument("--fee-maker", type=float, default=0.0002)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    recs = []
    for fp in sorted(glob.glob(args.vision)):
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)

    cfgs = list(sw.configs(args.mode))
    sim_fn = sw.simulate_ride if args.mode == "ride" else es.simulate_exit
    # results[ci] = list of (sym, gross, fee_units, risk, vscore, vdecision)
    results = [[] for _ in cfgs]

    store = Path(args.store)
    for (sym, date), setups in sorted(by_day.items()):
        t_ts, t_pr, _, _ = mc.load_trades_arr(store, sym, date)
        if len(t_ts) == 0:
            continue
        for r in setups:
            for ci, c in enumerate(cfgs):
                res = sim_fn(t_ts, t_pr, r["ts_close"], r["side"], r["level"], c)
                if res:
                    results[ci].append((sym, res["gross"], res["fee_units"], res["risk"],
                                        r["vscore"], r["vdecision"]))

    lines = []

    def emit(s=""):
        lines.append(s); print(s, flush=True)

    emit("=" * 112)
    emit(f"  VISION x EXIT  mode={args.mode}  n_setups={len(recs)}  configs={len(cfgs)}  "
         f"(re-walk, no API)")
    emit("=" * 112)

    for tag, fee in (("TAKER", args.fee_taker), ("MAKER", args.fee_maker)):
        emit(f"\n  ===== {tag} ({fee*2:.2%} round-trip) =====")
        # rank configs by the vision-TRADE-subset mean (the question: best exit for the picks)
        for sub_name, sub_f in SUBSETS:
            emit(f"\n  --- subset: {sub_name} ---   "
                 f"{'config':30s} {'n':>5s} {'mean%':>8s} {'med%':>8s} {'win%':>6s} "
                 f"{'sum%':>7s} {'coins+':>7s}")
            table = []
            for ci, c in enumerate(cfgs):
                rows = [(s, g, fu, rk) for (s, g, fu, rk, vs, vd) in results[ci] if sub_f(
                    {"vscore": vs, "vdecision": vd})]
                n, mean, med, win, tot, cp, nc = agg(rows, fee)
                if n:
                    table.append((mean, sw.cfg_name(c), n, med, win, tot, cp, nc))
            for mean, name, n, med, win, tot, cp, nc in sorted(table, reverse=True)[:6]:
                emit(f"  {'':24s} {name:30s} {n:5d} {mean*100:+8.3f} {med*100:+8.3f} "
                     f"{win:6.1%} {tot*100:+7.1f} {cp:3d}/{nc:<3d}")

    if args.out:
        Path(args.out).write_text("\n".join(lines))
        emit(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
