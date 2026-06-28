"""Phase 4 — runner-capture on the IMPROVED (spacing-clean) population.

Methodological fix: every prior entry/exit test (Phase 2 armed-cross, confirm-
continuum, Phase 1.5/1.9 exit sweeps) ran on the OLD detector population, BEFORE
the spacing-clean quality signal (Phase 3, one_sided>=0.9 & spacing>60) was found.
This re-runs the user's design ON the clean population:

  entry  : MARKET (taker) at the breakout close (accept the spike-top entry)
  manage : take f1 at +tp1 -> move stop to breakeven
  ride   : hold the remainder to the MAX via one of two exit rules --
             A) fixed trail (giveback from peak)            = Phase-3 baseline
             B) TIB exhaustion: exit when signed aggressor  = NEW lever
                flow over a rolling window reverses to a
                fraction of its peak favorable thrust
                (arb bots / sellers take over = run done)

Compares realized TAKER PnL, clean vs all, per month, coins+, by exit reason.
Runs on the server tape store (data/icebreaker_active, 9 coins, Mar-May).

    python scripts/research/icebreaker_runner_exit.py \
        --store /root/trading/data/icebreaker_active \
        --symbols 1000BONKUSDT ... --start 2026-03-01 --end 2026-05-31
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from bisect import bisect_left
from collections import Counter, deque
from datetime import datetime, timezone
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
major = _load("icebreaker_major", "scripts/research/icebreaker_major.py")
esim = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")


def spacing(touches, span_bars):
    if not touches or touches < 2 or span_bars is None:
        return None
    return span_bars / (touches - 1)


def is_clean(b):
    sp = spacing(b.get("touches"), b.get("span_bars"))
    return b.get("one_sided", 0) >= 0.9 and sp is not None and sp > 60


def simulate_tib(ts, px, qty, sd, ts0, side, level, cfg):
    """Market entry; f1@tp1->BE; ride remainder, exit on signed-flow exhaustion.

    Signed window flow `run` = sum(±qty) over the last tib_win_ms (buy=+, sell=-).
    Track peak favorable thrust; exit the remainder when flow reverses to
    -tib_frac * peak (the aggressor that drove the run has flipped)."""
    i = bisect_left(ts, ts0)
    if i >= len(ts):
        return None
    entry = float(px[i])
    dirn = 1.0 if side == "long" else -1.0
    stop0 = level * (1 - cfg["buffer"]) if side == "long" else level * (1 + cfg["buffer"])
    risk = dirn * (entry - stop0) / entry
    tp1_px = entry * (1 + dirn * cfg["tp1"])
    f1, W, frac = cfg["f1"], cfg["tib_win_ms"], cfg["tib_frac"]
    t_end = ts0 + cfg["horizon_ms"]

    legs, remaining, tp1_done = [], 1.0, False
    best = entry
    reason = "horizon"
    dq = deque()           # (ts, signed_qty) within window
    run = 0.0              # signed volume in window
    run_peak = 0.0        # peak favorable (dirn*run) thrust seen while riding
    j, last = i, entry
    while j < len(ts) and ts[j] <= t_end:
        p = float(px[j]); last = p
        sq = float(qty[j]) * (1.0 if sd[j] == 1 else -1.0)
        dq.append((ts[j], sq)); run += sq
        cutoff = ts[j] - W
        while dq and dq[0][0] < cutoff:
            run -= dq.popleft()[1]
        if dirn * (p - best) > 0:
            best = p
        if tp1_done:
            fav = dirn * run                       # favorable signed flow
            if fav > run_peak:
                run_peak = fav
            if dirn * (p - entry) <= 0:            # BE stop on remainder
                legs.append((remaining, entry)); remaining = 0.0; reason = "be"; break
            if run_peak > 0 and fav < -frac * run_peak:   # flow exhausted/reversed
                legs.append((remaining, p)); remaining = 0.0; reason = "tib"; break
        else:
            if dirn * (p - stop0) <= 0:
                legs.append((remaining, stop0)); remaining = 0.0; reason = "stop"; break
            if dirn * (p - tp1_px) >= 0:
                legs.append((f1, tp1_px)); remaining -= f1; tp1_done = True; best = p
        j += 1
    if remaining > 1e-9:
        legs.append((remaining, last))

    gross = sum(g * dirn * (pp - entry) / entry for g, pp in legs)
    fee_units = 1.0 + sum(g for g, _ in legs)
    net_taker = gross - cfg["fee_side"] * fee_units
    return {"gross": gross, "fee_units": fee_units, "risk": risk,
            "R": (net_taker / risk) if risk > 1e-9 else float("nan"),
            "reason": reason, "mfe": dirn * (best - entry) / entry, "net": net_taker}


def summarize(name, rows):
    if not rows:
        print(f"  {name}: no trades"); return
    n = len(rows)
    nets = [r["net"] for r in rows]
    mean = sum(nets) / n
    wins = sum(1 for x in nets if x > 0)
    reasons = Counter(r["reason"] for r in rows)
    pos = sum(nets)
    print(f"  {name:<22} n={n:<4} taker_mean={mean*100:+.3f}% sum={pos*100:+.1f}% "
          f"WR={wins/n*100:.0f}% {dict(reasons)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--fee-side", type=float, default=0.00055)
    p.add_argument("--buffer", type=float, default=0.001)
    p.add_argument("--tp1", type=float, default=0.006)
    p.add_argument("--f1", type=float, default=0.5)
    p.add_argument("--trail-giveback", type=float, default=0.004)
    p.add_argument("--horizon-ms", type=int, default=1_800_000)
    p.add_argument("--tib-win-ms", type=int, default=15_000)
    p.add_argument("--tib-frac", type=float, default=0.5)
    args = p.parse_args()

    base_cfg = {"buffer": args.buffer, "tp1": args.tp1, "f1": args.f1,
                "trail_giveback": args.trail_giveback, "horizon_ms": args.horizon_ms,
                "fee_side": args.fee_side}
    tib_cfg = dict(base_cfg, tib_win_ms=args.tib_win_ms, tib_frac=args.tib_frac)

    store = Path(args.store)
    # collect per-trade results, tagged month / clean
    buckets = {(pop, ex): [] for pop in ("ALL", "CLEAN") for ex in ("trail", "tib")}
    month_buckets = {}

    for sym in args.symbols:
        # continuous tape + bars across the whole range
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
        t_qty = np.concatenate(t_qty); t_sd = np.concatenate(t_sd)
        bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)
        bks = major.detect_major_breakouts(
            bars, args.bar_ms, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
            min_span_bars=120, brk=0.0015, cooldown=30, near_bars=30,
            near_tol=0.003, one_sided=0.70)
        nc = 0
        for bk in bks:
            ts0 = bk["ts_close"]; side = bk["side"]; lvl = bk["level"]
            r_trail = esim.simulate_exit(t_ts, t_pr, ts0, side, lvl, base_cfg)
            r_tib = simulate_tib(t_ts, t_pr, t_qty, t_sd, ts0, side, lvl, tib_cfg)
            if r_trail is None or r_tib is None:
                continue
            # esim returns net_taker implicitly via R; recompute net for consistency
            r_trail_net = r_trail["gross"] - args.fee_side * r_trail["fee_units"]
            r_trail = dict(r_trail, net=r_trail_net)
            mon = datetime.fromtimestamp(ts0 / 1000, timezone.utc).strftime("%Y-%m")
            clean = is_clean(bk)
            for ex, r in (("trail", r_trail), ("tib", r_tib)):
                buckets[("ALL", ex)].append(r)
                month_buckets.setdefault((mon, "ALL", ex), []).append(r)
                if clean:
                    buckets[("CLEAN", ex)].append(r)
                    month_buckets.setdefault((mon, "CLEAN", ex), []).append(r)
                    if ex == "trail":
                        nc += 1
        print(f"  [{sym}] setups={len(bks)} clean={nc}", flush=True)

    print("\n" + "=" * 78)
    print("POOLED (3 months) — market entry, f1@tp1->BE, ride")
    print("=" * 78)
    for pop in ("ALL", "CLEAN"):
        for ex in ("trail", "tib"):
            summarize(f"{pop}/{ex}", buckets[(pop, ex)])

    print("\n" + "=" * 78)
    print("PER MONTH")
    print("=" * 78)
    for mon in sorted(set(k[0] for k in month_buckets)):
        print(f"\n-- {mon} --")
        for pop in ("ALL", "CLEAN"):
            for ex in ("trail", "tib"):
                summarize(f"{pop}/{ex}", month_buckets.get((mon, pop, ex), []))


if __name__ == "__main__":
    main()
