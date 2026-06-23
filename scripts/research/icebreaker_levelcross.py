"""Honest level-cross entry (Phase 2b): arm a gated level, enter taker on the FIRST
cross — no close-confirmation, so fakeout-pokes are included.

The counterfactual (entry_cf) proved entering AT the level pays (payoff~2.7), and the
in-candle cross on CONFIRMED setups captured ~80% of it (+0.28%/trade) — but that uses
close-confirmation (you only know it broke after the candle closes). This removes the
lookahead: detect_setups(arm_only=True) emits quality levels the moment price is NEAR
them (all gates pass, computed from PRIOR bars — no future), then we enter on the first
tape cross within a window. Levels that only get poked and revert ARE included — the
honest, taker-fillable population. The question: does the +0.28% ceiling survive the
fakeout cost?

Mirrors icebreaker_robust's basis: CONTINUOUS month bars per coin, ts_close = bar end.

    python scripts/research/icebreaker_levelcross.py \
        --store /root/trading/data/icebreaker_active --cache /root/trading/tmp/ib_liq_empty.jsonl \
        --start 2026-03-01 --end 2026-03-31 --tag MARCH
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
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
pd = _load("icebreaker_pnl_decomp", "scripts/research/icebreaker_pnl_decomp.py")
from src.strategy import robust_levels as rl

EXIT_CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000}


def cross_trade(t_ts, t_pr, start_ts, side, level, window_ms, cfg):
    """First tape cross of `level` at/after start_ts within window -> managed trade
    from that taker fill. None if the level is never crossed in the window (no trade)."""
    i = bisect_left(t_ts, start_ts)
    end = start_ts + window_ms
    long = side == "long"
    while i < len(t_ts) and t_ts[i] <= end:
        p = float(t_pr[i])
        if (long and p >= level) or (not long and p <= level):
            return mf.walk_exit(t_ts, t_pr, i, p, side, level, cfg)
        i += 1
    return None


def to_rec(res, sym):
    return {"gross": res["gross"], "fee_units": 1.0 + res["exit_units"],
            "reason_exit": res["reason"], "mfe": res["mfe"], "symbol": sym}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", default="", help="dump to derive the coin universe from")
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-31")
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--window-bars", type=int, default=30, help="bars to wait for a cross")
    p.add_argument("--fee", type=float, default=0.00055)
    p.add_argument("--confirmed", action="store_true", help="also run confirmed setups (cross entry) for the no-fakeout baseline")
    p.add_argument("--tag", default="")
    args = p.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        recs = [json.loads(l) for l in open(args.cache) if l.strip()]
        symbols = sorted(set(r["symbol"] for r in recs))
    store = Path(args.store)
    window_ms = args.window_bars * args.bar_ms

    armed_rows, conf_rows = [], []
    n_armed = n_trig = n_conf = 0
    for sym in symbols:
        t_ts, t_pr, t_qty = [], [], []
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            a, b, c, _ = mc.load_trades_arr(store, sym, date)
            if len(a):
                t_ts.append(a); t_pr.append(b); t_qty.append(c)
        if not t_ts:
            print(f"  {sym}: no tape", flush=True); continue
        t_ts = np.concatenate(t_ts); t_pr = np.concatenate(t_pr); t_qty = np.concatenate(t_qty)
        bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)

        armed = rl.detect_setups(bars, arm_only=True)
        st = 0
        for s in armed:
            n_armed += 1
            start = bars[s.idx]["ts"] + args.bar_ms        # arm at bar close, trigger after
            res = cross_trade(t_ts, t_pr, start, s.side, s.level, window_ms, EXIT_CFG)
            if res:
                n_trig += 1; armed_rows.append(to_rec(res, sym))
        if args.confirmed:
            conf = rl.detect_setups(bars)                  # default = close-confirmed
            for s in conf:
                n_conf += 1
                start = bars[s.idx]["ts"]                  # cross inside the break candle
                res = cross_trade(t_ts, t_pr, start, s.side, s.level, args.bar_ms, EXIT_CFG)
                if res:
                    conf_rows.append(to_rec(res, sym))
        print(f"  {sym}: armed={len(armed)} triggered={sum(1 for r in armed_rows if r['symbol']==sym)}"
              + (f" confirmed={sum(1 for r in conf_rows if r['symbol']==sym)}" if args.confirmed else ""),
              flush=True)

    print("\n" + "#" * 90)
    print(f"  LEVEL-CROSS (honest, fakeouts included)  {args.tag}  symbols={len(symbols)}  "
          f"window={args.window_bars}bars")
    print(f"  armed levels={n_armed}  triggered (crossed)={n_trig} ({n_trig/n_armed*100 if n_armed else 0:.0f}%)  "
          f"expired={n_armed-n_trig}")
    print("#" * 90)
    pd.decomp(f"{args.tag} ARMED-cross", armed_rows, args.fee)
    if args.confirmed:
        print(f"\n  (baseline: confirmed setups, cross entry — no fakeouts, n_conf={n_conf})")
        pd.decomp(f"{args.tag} CONFIRMED-cross", conf_rows, args.fee)


if __name__ == "__main__":
    main()
