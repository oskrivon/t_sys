"""Exit-config sweep on gated breakout setups — one tape pass, many configs.

icebreaker_exit_sim.py answers "is config X tradeable" but reloads the 389MB tape
per run. To find the OPTIMAL exit we need a grid, so this walks each (symbol,date)
tape ONCE and simulates every config on it, accumulating realized PnL/R.

Reports, per config (sorted by meanR at maker): n, win%, meanR, mean/median net%,
sum%, at taker AND maker fees, plus the per-coin sign-spread (how many of the 9
coins are net-positive — robustness, not one-coin overfit).

    python scripts/research/icebreaker_exit_sweep.py \
        --store /root/trading/data/icebreaker_active \
        --cache 'tmp/ib_gated_*.jsonl' --out tmp/ib_exit_sweep.txt
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import itertools
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


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")
es = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")

from bisect import bisect_left


def simulate_ride(ts, px, ts0, side, level, cfg):
    """Scalper 'de-risk then ride': take a small partial at +t1 -> move stop to BE,
    then RIDE the remainder, exiting only when price retraces `gb_frac` of the OPEN
    PROFIT from the peak (a percent-of-move trail that widens with the move, so big
    breakouts are ridden and small ones exit near BE). Same return shape as
    icebreaker_exit_sim.simulate_exit."""
    i = bisect_left(ts, ts0)
    if i >= len(ts):
        return None
    entry = float(px[i])
    dirn = 1.0 if side == "long" else -1.0
    stop0 = level * (1 - cfg["buffer"]) if side == "long" else level * (1 + cfg["buffer"])
    risk = dirn * (entry - stop0) / entry
    t1_px = entry * (1 + dirn * cfg["t1"])
    f1, gbf = cfg["f1"], cfg["gb_frac"]
    t_end = ts0 + cfg["horizon_ms"]

    legs, remaining, partial_done, best = [], 1.0, False, entry
    reason, j, last = "horizon", i, entry
    while j < len(ts) and ts[j] <= t_end:
        p = float(px[j]); last = p
        if dirn * (p - best) > 0:
            best = p
        if partial_done:
            # give back gb_frac of the open profit from the peak, never below BE.
            # trail = best - gbf*(best-entry) is correct for BOTH sides (best sits on
            # the favorable side, so best-entry carries the sign).
            trail = best - gbf * (best - entry)
            eff = max(trail, entry) if side == "long" else min(trail, entry)
            if dirn * (p - eff) <= 0:
                legs.append((remaining, eff)); remaining = 0.0; reason = "ride"; break
        else:
            if dirn * (p - stop0) <= 0:
                legs.append((remaining, stop0)); remaining = 0.0; reason = "stop"; break
            if dirn * (p - t1_px) >= 0:
                legs.append((f1, t1_px)); remaining -= f1; partial_done = True; best = p
        j += 1
    if remaining > 1e-9:
        legs.append((remaining, last))

    gross = sum(g * dirn * (pp - entry) / entry for g, pp in legs)
    fee_units = 1.0 + sum(g for g, _ in legs)
    net_taker = gross - cfg["fee_side"] * fee_units
    return {"gross": gross, "fee_units": fee_units, "risk": risk,
            "R": (net_taker / risk) if risk > 1e-9 else float("nan"),
            "reason": reason, "mfe": dirn * (best - entry) / entry}


# grid axes (only the levers that matter for "can we be positive")
BUFFERS = [0.001, 0.002, 0.004]        # stop distance behind the level
TP1S = [0.004, 0.006, 0.010]           # first scale-out target
TRAILS = [0.004, 0.008]                # giveback from peak on the runner
HORIZONS = [900_000, 1_800_000]        # 15m / 30m hold
F1 = 0.5                               # fraction taken at TP1

# let-it-run: no fixed TP, pure trailing stop from entry (tp1=0,f1=0 -> TP1 fires at
# entry with zero size, then the whole position trails the peak). Wider horizons.
LR_BUFFERS = [0.002, 0.004]
LR_TRAILS = [0.003, 0.005, 0.008, 0.012]
LR_HORIZONS = [900_000, 1_800_000, 3_600_000]   # 15 / 30 / 60m


# ride mode: small de-risk partial then ride, exiting on a % -of-open-profit retrace
RIDE_BUFFERS = [0.002, 0.004]
RIDE_T1 = [0.003, 0.005]          # partial target (de-risk to BE)
RIDE_F1 = [0.3, 0.5]              # fraction taken at the partial
RIDE_GBF = [0.3, 0.4, 0.5]        # give back this fraction of peak open-profit
RIDE_HORIZONS = [1_800_000, 3_600_000, 7_200_000]   # 30 / 60 / 120m (ride needs room)


def configs(mode):
    if mode == "letrun":
        for buf, tr, hz in itertools.product(LR_BUFFERS, LR_TRAILS, LR_HORIZONS):
            yield {"buffer": buf, "tp1": 0.0, "f1": 0.0, "trail_giveback": tr,
                   "horizon_ms": hz, "fee_side": 0.0}
    elif mode == "ride":
        for buf, t1, f1, gbf, hz in itertools.product(
                RIDE_BUFFERS, RIDE_T1, RIDE_F1, RIDE_GBF, RIDE_HORIZONS):
            yield {"buffer": buf, "t1": t1, "f1": f1, "gb_frac": gbf,
                   "horizon_ms": hz, "fee_side": 0.0, "ride": True}
    else:
        for buf, tp1, tr, hz in itertools.product(BUFFERS, TP1S, TRAILS, HORIZONS):
            yield {"buffer": buf, "tp1": tp1, "f1": F1, "trail_giveback": tr,
                   "horizon_ms": hz, "fee_side": 0.0}   # fee applied later per-tag


def cfg_name(c):
    if c.get("ride"):
        return (f"buf{c['buffer']*100:.1f} t1{c['t1']*100:.1f} f{c['f1']:.1f} "
                f"gbf{c['gb_frac']:.1f} h{c['horizon_ms']//60000}")
    return (f"buf{c['buffer']*100:.1f} tp{c['tp1']*100:.1f} tr{c['trail_giveback']*100:.1f} "
            f"h{c['horizon_ms']//60000}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True, help="glob of per-coin gated dumps")
    p.add_argument("--fee-taker", type=float, default=0.00055)
    p.add_argument("--fee-maker", type=float, default=0.0002)
    p.add_argument("--mode", choices=["managed", "letrun", "ride"], default="managed")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    files = sorted(glob.glob(args.cache))
    recs = []
    for fp in files:
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)
    cfgs = list(configs(args.mode))
    # results[ci] = list of (symbol, gross, fee_units, risk)
    results = [[] for _ in cfgs]

    store = Path(args.store)
    for (sym, date), setups in sorted(by_day.items()):
        t_ts, t_pr, _, _ = mc.load_trades_arr(store, sym, date)
        if len(t_ts) == 0:
            continue
        sim_fn = simulate_ride if args.mode == "ride" else es.simulate_exit
        for r in setups:
            for ci, c in enumerate(cfgs):
                res = sim_fn(t_ts, t_pr, r["ts_close"], r["side"], r["level"], c)
                if res:
                    results[ci].append((sym, res["gross"], res["fee_units"], res["risk"]))

    lines = []

    def emit(s=""):
        lines.append(s)
        print(s, flush=True)

    n_total = len(recs)
    emit("=" * 104)
    emit(f"  EXIT SWEEP  n_setups={n_total}  coins={len(set(r['symbol'] for r in recs))}  "
         f"configs={len(cfgs)}  (f1={F1:.0%})")
    emit("=" * 104)

    for tag, fee in (("TAKER", args.fee_taker), ("MAKER", args.fee_maker)):
        emit(f"\n  --- {tag}  ({fee*2:.2%} round-trip) ---  sorted by meanR")
        emit(f"  {'config':28s} {'n':>5s} {'win%':>6s} {'meanR':>7s} {'meanNet%':>9s} "
             f"{'medNet%':>8s} {'sum%':>7s} {'coins+':>7s}")
        table = []
        for ci, c in enumerate(cfgs):
            rows = results[ci]
            if not rows:
                continue
            nets = [g - fee * fu for _, g, fu, _ in rows]
            Rs = [(g - fee * fu) / rk for _, g, fu, rk in rows if rk > 1e-9]
            meanR = sum(Rs) / len(Rs) if Rs else float("nan")
            n = len(nets)
            win = sum(1 for x in nets if x > 0) / n
            mean = sum(nets) / n
            med = sorted(nets)[n // 2]
            # per-coin net sum -> how many coins are positive
            per = defaultdict(float)
            for (sym, g, fu, _), net in zip(rows, nets):
                per[sym] += net
            coins_pos = sum(1 for v in per.values() if v > 0)
            table.append((meanR, cfg_name(c), n, win, mean, med, sum(nets),
                          coins_pos, len(per)))
        for meanR, name, n, win, mean, med, tot, cp, nc in sorted(table, reverse=True):
            emit(f"  {name:28s} {n:5d} {win:6.1%} {meanR:+7.2f} {mean*100:+9.3f} "
                 f"{med*100:+8.3f} {tot*100:+7.1f} {cp:3d}/{nc:<3d}")

    if args.out:
        args.out.write_text("\n".join(lines))
        emit(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
