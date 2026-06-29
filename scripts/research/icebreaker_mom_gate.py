"""Final-round task (1) DECISIVE: does the break-candle momentum (mom-k0) that separates
fat winners (AUC 0.73, robust 3/3 months — icebreaker_fatwinner_vel/_lag.py) convert to
realized PnL, or does it live only in favorable EXCURSION (the recurring icebreaker wall)?

mom-k0 is knowable EXACTLY at the entry point (breakout close, no lookahead, ~ms to compute
live). We gate the same market-entry / managed-exit design (Phase 4 runner_exit) on it and
split realized TAKER PnL by mom-k0 tercile. If the high-mom tercile crosses gross > 2x costs
-> tradeable selection. If high MFE but net still sub-cost -> excursion-not-PnL wall, closed
with a number.

Reuses detect_major_breakouts + simulate_exit (trail) + simulate_tib from runner_exit.
mom-k0 = signed 30m (30x1m) return ending at the break bar.

    python scripts/research/icebreaker_mom_gate.py \
        --store /root/trading/data/icebreaker_active \
        --symbols 1000BONKUSDT 1000FLOKIUSDT 1000PEPEUSDT DOGEUSDT FARTCOINUSDT \
                  ORDIUSDT POPCATUSDT WIFUSDT WLDUSDT \
        --start 2026-03-01 --end 2026-05-31
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import defaultdict
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
rex = _load("icebreaker_runner_exit", "scripts/research/icebreaker_runner_exit.py")

MOM_WIN = 30          # 30 x 1m = 30m momentum window ending at the break bar


def report(name, rows, key):
    if not rows:
        print(f"  {name}: no trades"); return
    n = len(rows)
    nets = [r[key] for r in rows]
    mfes = [r["mfe"] for r in rows]
    mean = sum(nets) / n
    gross_mean = sum(r["gross"] for r in rows) / n
    wins = sum(1 for x in nets if x > 0)
    coins = defaultdict(float)
    for r in rows:
        coins[r["sym"]] += r[key]
    cplus = sum(1 for v in coins.values() if v > 0)
    print(f"  {name:<26} n={n:<4} net={mean*100:+.3f}% gross={gross_mean*100:+.3f}% "
          f"WR={wins/n*100:.0f}% medMFE={sorted(mfes)[n//2]*100:.2f}% coins+={cplus}/{len(coins)}")


def split_terciles(rows, mom_key="mom"):
    vals = sorted(r[mom_key] for r in rows)
    if len(vals) < 3:
        return {}
    lo = vals[len(vals) // 3]
    hi = vals[2 * len(vals) // 3]
    out = {"LOW": [], "MID": [], "HIGH": []}
    for r in rows:
        m = r[mom_key]
        out["LOW" if m <= lo else "HIGH" if m > hi else "MID"].append(r)
    return out


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
    p.add_argument("--dump", default="", help="write per-setup rows to this jsonl")
    args = p.parse_args()

    base_cfg = {"buffer": args.buffer, "tp1": args.tp1, "f1": args.f1,
                "trail_giveback": args.trail_giveback, "horizon_ms": args.horizon_ms,
                "fee_side": args.fee_side}
    tib_cfg = dict(base_cfg, tib_win_ms=args.tib_win_ms, tib_frac=args.tib_frac)
    store = Path(args.store)

    rows = []          # one row per setup, both exits + mom
    for sym in args.symbols:
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
        bar_ts = np.array([b["ts"] for b in bars])
        bar_cl = np.array([b["close"] for b in bars])
        bks = major.detect_major_breakouts(
            bars, args.bar_ms, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
            min_span_bars=120, brk=0.0015, cooldown=30, near_bars=30,
            near_tol=0.003, one_sided=0.70)
        kept = 0
        for bk in bks:
            ts0 = bk["ts_close"]; side = bk["side"]; lvl = bk["level"]
            bidx = int(np.searchsorted(bar_ts, ts0))
            if bidx >= len(bar_ts) or bar_ts[bidx] != ts0 or bidx < MOM_WIN:
                continue
            sign = 1.0 if side == "long" else -1.0
            mom = sign * (bar_cl[bidx] / bar_cl[bidx - MOM_WIN] - 1.0)
            r_trail = esim.simulate_exit(t_ts, t_pr, ts0, side, lvl, base_cfg)
            r_tib = rex.simulate_tib(t_ts, t_pr, t_qty, t_sd, ts0, side, lvl, tib_cfg)
            if r_trail is None or r_tib is None:
                continue
            net_trail = r_trail["gross"] - args.fee_side * r_trail["fee_units"]
            mon = datetime.fromtimestamp(ts0 / 1000, timezone.utc).strftime("%Y-%m")
            rows.append({
                "sym": sym, "month": mon, "mom": mom, "clean": rex.is_clean(bk),
                "net_trail": net_trail, "gross_trail": r_trail["gross"],
                "net_tib": r_tib["net"], "gross_tib": r_tib["gross"],
                "mfe": r_trail["mfe"],
            })
            kept += 1
        print(f"  [{sym}] setups={len(bks)} kept={kept}", flush=True)

    if args.dump:
        import json as _json
        with open(args.dump, "w") as fh:
            for r in rows:
                fh.write(_json.dumps(r) + "\n")
        print(f"dumped {len(rows)} rows -> {args.dump}")

    print(f"\nTOTAL setups with mom = {len(rows)}")
    for ex in ("trail", "tib"):
        nk, gk = f"net_{ex}", f"gross_{ex}"
        # adapt report() keys per exit
        def rep(name, rs):
            if not rs:
                print(f"  {name}: no trades"); return
            n = len(rs)
            nets = [r[nk] for r in rs]; mean = sum(nets) / n
            gmean = sum(r[gk] for r in rs) / n
            wins = sum(1 for x in nets if x > 0)
            mfes = sorted(r["mfe"] for r in rs)
            coins = defaultdict(float)
            for r in rs:
                coins[r["sym"]] += r[nk]
            cplus = sum(1 for v in coins.values() if v > 0)
            print(f"  {name:<24} n={n:<4} net={mean*100:+.3f}% gross={gmean*100:+.3f}% "
                  f"WR={wins/n*100:.0f}% medMFE={mfes[n//2]*100:.2f}% coins+={cplus}/{len(coins)}")
        print("\n" + "=" * 84)
        print(f"EXIT = {ex.upper()}  (entry=market@break-close, f1@+0.6->BE, ride)")
        print("=" * 84)
        rep("ALL", rows)
        ter = split_terciles(rows)
        for b in ("LOW", "MID", "HIGH"):
            rep(f"mom-{b}", ter.get(b, []))
        cl = [r for r in rows if r["clean"]]
        rep("CLEAN", cl)
        terc = split_terciles(cl)
        for b in ("LOW", "MID", "HIGH"):
            rep(f"CLEAN+mom-{b}", terc.get(b, []))
        # high-mom per month robustness
        print("  -- HIGH-mom by month --")
        hi = ter.get("HIGH", [])
        for mon in sorted(set(r["month"] for r in hi)):
            rep(f"  {mon}", [r for r in hi if r["month"] == mon])


if __name__ == "__main__":
    main()
