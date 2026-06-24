"""Confirmation continuum (Phase 2 lever 'д'): the only honest lever left after the
real/fakeout discriminator turned up empty in BOTH the tape (Angle 1) and the book
(Angle 2). At the instant of touch you can't tell a real break from a fakeout-poke; the
two endpoints are known and both <= breakeven:

  raw armed-cross (hold 0, delta 0):  WR 19%, GOOD price (payoff ~2.1), exp -0.086%
  close-confirmation (full 60s bar):  WR 57%, BAD price (entry at spike top), exp ~0

Between them lies a tunable, lookahead-FREE confirmation: arm the gated level, and on the
first cross require the break to HOLD the break side for `hold_ms` and stand >= `delta`
bps beyond the level at t0+hold_ms before entering taker at that (worse) price. Waiting
rejects the pokes that revert (cuts the 81% fakeouts) but you pay give-back + a wider stop
(stop stays at level-buffer, anchored to the level). The question: does WR rise faster
than price decays anywhere on the (hold x delta) grid -> a positive, fillable sweet-spot?

Tape-only (no book) -> runs on ALL 9 coins for BOTH March (in-sample) and April (OOS).

    python scripts/research/icebreaker_confirm_sweep.py \
        --store /root/trading/data/icebreaker_active \
        --cache /root/trading/tmp/ib_liq_empty.jsonl \
        --start 2026-03-01 --end 2026-03-31 --tag MARCH
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
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
mf = _load("icebreaker_maker_fill", "scripts/research/icebreaker_maker_fill.py")
from src.strategy import robust_levels as rl

EXIT_CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000}
HOLDS_MS = [0, 2000, 5000, 10000, 30000]
DELTAS = [0.0, 0.0010, 0.0020]


def first_cross(t_ts, t_pr, start_ts, side, level, window_ms):
    """Index/ts of the first tape cross of `level` in [start, start+window]. None if never."""
    i = bisect_left(t_ts, start_ts)
    end = start_ts + window_ms
    long = side == "long"
    while i < len(t_ts) and t_ts[i] <= end:
        p = float(t_pr[i])
        if (long and p >= level) or (not long and p <= level):
            return i
        i += 1
    return None


def confirm_idx(t_ts, t_pr, ci0, side, level, hold_ms, delta):
    """From the first cross at ci0, apply the (hold_ms, delta) confirmation with NO
    lookahead past t0+hold_ms. Returns the entry index, or None if the break is rejected
    (reverted through the level = poke, or stands < delta beyond at t0+hold_ms)."""
    long = side == "long"
    if hold_ms == 0 and delta == 0.0:
        return ci0
    t0 = t_ts[ci0]
    t_c = t0 + hold_ms
    thr = level * (1 + delta) if long else level * (1 - delta)
    j = ci0 + 1
    while j < len(t_ts) and t_ts[j] < t_c:           # hold window: reject if it gives back
        p = float(t_pr[j])
        if (long and p < level) or (not long and p > level):
            return None
        j += 1
    if j >= len(t_ts):
        return None
    p = float(t_pr[j])                               # first tick at/after t0+hold_ms
    if (long and p >= thr) or (not long and p <= thr):
        return j
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", default="")
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-31")
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--window-bars", type=int, default=30)
    p.add_argument("--fee", type=float, default=0.00055)
    p.add_argument("--tag", default="")
    args = p.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        recs = [json.loads(l) for l in open(args.cache) if l.strip()]
        symbols = sorted(set(r["symbol"] for r in recs))
    store = Path(args.store)
    window_ms = args.window_bars * args.bar_ms

    # cfg -> list of (symbol, net, mfe, give_back, raw_good); plus per-cfg fill bookkeeping
    cfg_rows = {(h, d): [] for h in HOLDS_MS for d in DELTAS}
    armed_total = 0
    raw_kept = {(h, d): {"drop_good": 0, "drop_n": 0} for h in HOLDS_MS for d in DELTAS}

    for sym in symbols:
        t_ts, t_pr, t_qty, t_sd = [], [], [], []
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            a, b, c, d = mc.load_trades_arr(store, sym, date)
            if len(a):
                t_ts.append(a); t_pr.append(b); t_qty.append(c); t_sd.append(d)
        if not t_ts:
            print(f"  {sym}: no tape", flush=True); continue
        t_ts = np.concatenate(t_ts); t_pr = np.concatenate(t_pr)
        bars = mc.bars_np(t_ts, t_pr, np.concatenate(t_qty), args.bar_ms)
        armed = rl.detect_setups(bars, arm_only=True)
        armed_total += len(armed)

        for s in armed:
            start = bars[s.idx]["ts"] + args.bar_ms
            ci0 = first_cross(t_ts, t_pr, start, s.side, s.level, window_ms)
            if ci0 is None:
                continue
            # raw outcome (entry at level/touch) = the setup's "true nature" for adverse-sel
            raw = mf.walk_exit(t_ts, t_pr, ci0, float(t_pr[ci0]), s.side, s.level, EXIT_CFG)
            raw_good = raw["mfe"] >= 0.01
            for h in HOLDS_MS:
                for dlt in DELTAS:
                    ei = confirm_idx(t_ts, t_pr, ci0, s.side, s.level, h, dlt)
                    if ei is None:
                        raw_kept[(h, dlt)]["drop_n"] += 1
                        raw_kept[(h, dlt)]["drop_good"] += int(raw_good)
                        continue
                    ep = float(t_pr[ei])
                    res = mf.walk_exit(t_ts, t_pr, ei, ep, s.side, s.level, EXIT_CFG)
                    gb = (ep - s.level) / s.level if s.side == "long" else (s.level - ep) / s.level
                    net = res["gross"] - args.fee * (1.0 + res["exit_units"])
                    cfg_rows[(h, dlt)].append((sym, net, res["mfe"], gb, raw_good))
        print(f"  {sym}: armed={len(armed)}", flush=True)

    print("\n" + "#" * 100)
    print(f"  CONFIRMATION CONTINUUM  {args.tag}  symbols={len(symbols)}  armed={armed_total}")
    print(f"  (entry = taker at first tick >= delta bps beyond level, t0+hold; stop@level-buffer; exit buf0.1/tp0.6/tr0.4/h30)")
    print("#" * 100)
    print(f"  {'hold':>5s} {'delta':>6s} {'fill%':>6s} {'n':>5s} {'gback%':>7s} {'WR':>5s} "
          f"{'exp_net%':>9s} {'payoff':>7s} {'coins+':>7s} {'kept_gr':>8s} {'drop_gr':>8s}")
    for h in HOLDS_MS:
        for dlt in DELTAS:
            rows = cfg_rows[(h, dlt)]
            n = len(rows)
            if n == 0:
                print(f"  {h/1000:5.0f}s {dlt*1e4:5.0f}b {0:6.0%} {0:5d}      -     -         -       -       -        -        -")
                continue
            nets = [r[1] for r in rows]
            wins = [x for x in nets if x > 0]; los = [x for x in nets if x <= 0]
            aw = sum(wins) / len(wins) if wins else 0.0
            al = sum(los) / len(los) if los else 0.0
            gb = sum(r[3] for r in rows) / n
            kept_gr = sum(1 for r in rows if r[4]) / n
            dn = raw_kept[(h, dlt)]["drop_n"]; dg = raw_kept[(h, dlt)]["drop_good"]
            drop_gr = dg / dn if dn else 0.0
            bycoin = defaultdict(list)
            for r in rows:
                bycoin[r[0]].append(r[1])
            cplus = sum(1 for v in bycoin.values() if sum(v) > 0)
            fillpct = n / armed_total if armed_total else 0.0
            print(f"  {h/1000:5.0f}s {dlt*1e4:5.0f}b {fillpct:6.0%} {n:5d} {gb*100:7.3f} "
                  f"{len(wins)/n:5.0%} {sum(nets)/n*100:+9.3f} {abs(aw/al) if al else 0:7.2f} "
                  f"{cplus:3d}/{len(bycoin):<3d} {kept_gr:8.1%} {drop_gr:8.1%}")


if __name__ == "__main__":
    main()
