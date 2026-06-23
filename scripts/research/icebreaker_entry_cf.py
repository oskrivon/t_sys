"""Entry-geometry counterfactual: close-of-breakout vs ideal fill AT the level.

Phase-1.9 decomposition showed the killer is payoff ~1.0: entering at the breakout
CANDLE CLOSE (top of the spike) puts you ~0.5% above the invalidation, so every stop
is a full ~0.5% loss and the runner must clear that first. This isolates the entry
geometry: re-walk the SAME managed exit from the SAME moment (ts_close) but priced at
two entries —
  - close : entry = first trade at/after the break (today's model)
  - level : entry = the level itself (ideal retest fill; IGNORES fill-risk = the ceiling)
and reports the full PnL decomposition for each. If even the ideal level entry is thin,
the geometry doesn't save it. If level entry is clearly positive (payoff>1), the whole
game collapses to "can you fill at the level without adverse selection" (Phase 1.8 = no).

    python scripts/research/icebreaker_entry_cf.py \
        --store /root/trading/data/icebreaker_active --cache /root/trading/tmp/ib_liq_empty.jsonl --tag MAR
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
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
mf = _load("icebreaker_maker_fill", "scripts/research/icebreaker_maker_fill.py")
pd = _load("icebreaker_pnl_decomp", "scripts/research/icebreaker_pnl_decomp.py")

EXIT_CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000}


def to_rec(res):
    """walk_exit result -> the shape pnl_decomp.decomp expects."""
    return {"gross": res["gross"], "fee_units": 1.0 + res["exit_units"],
            "reason_exit": res["reason"], "mfe": res["mfe"]}


def first_cross_idx(ts, px, ts0, bar_ms, side, level):
    """First tape index INSIDE the breakout candle [ts0-bar_ms, ts0] where price
    reaches the level in the break direction (long: p>=level from below; short:
    p<=level from above). This is the real-time, taker-fillable entry — the moment
    a pre-armed level is touched, not the candle close. None if no touch in window."""
    lo = ts0 - bar_ms
    i = bisect_left(ts, lo)
    long = side == "long"
    while i < len(ts) and ts[i] <= ts0:
        p = float(px[i])
        if (long and p >= level) or (not long and p <= level):
            return i
        i += 1
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--fee", type=float, default=0.00055)
    p.add_argument("--bar-ms", type=int, default=60_000, help="breakout candle size")
    p.add_argument("--tag", default="")
    args = p.parse_args()

    recs = [json.loads(l) for l in open(args.cache) if l.strip()]
    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    close_rows, level_rows, cross_rows = [], [], []
    n_nocross = 0
    for (sym, date), setups in sorted(by_day.items()):
        t_ts, t_pr, _, _ = mc.load_trades_arr(store, sym, date)
        if len(t_ts) == 0:
            continue
        for r in setups:
            i = bisect_left(t_ts, r["ts_close"])
            if i >= len(t_ts):
                continue
            close_px = float(t_pr[i])
            rc = mf.walk_exit(t_ts, t_pr, i, close_px, r["side"], r["level"], EXIT_CFG)
            rl = mf.walk_exit(t_ts, t_pr, i, r["level"], r["side"], r["level"], EXIT_CFG)
            close_rows.append({**to_rec(rc), "symbol": sym})
            level_rows.append({**to_rec(rl), "symbol": sym})
            # cross: enter at the first touch of the level inside the breakout candle
            ci = first_cross_idx(t_ts, t_pr, r["ts_close"], args.bar_ms, r["side"], r["level"])
            if ci is None:
                n_nocross += 1
                continue
            rx = mf.walk_exit(t_ts, t_pr, ci, float(t_pr[ci]), r["side"], r["level"], EXIT_CFG)
            cross_rows.append({**to_rec(rx), "symbol": sym})

    print(f"\n################  ENTRY = CLOSE (today's model)  —  {args.tag}  ################")
    pd.decomp(f"{args.tag} close", close_rows, args.fee)
    print(f"\n################  ENTRY = LEVEL (ideal fill, ignores fill-risk)  —  {args.tag}  ################")
    pd.decomp(f"{args.tag} level", level_rows, args.fee)
    print(f"\n################  ENTRY = CROSS (taker at first level-touch in the break candle)  —  {args.tag}  ################")
    print(f"  (entered {len(cross_rows)} / {len(close_rows)} setups; {n_nocross} had no in-candle touch)")
    pd.decomp(f"{args.tag} cross", cross_rows, args.fee)


if __name__ == "__main__":
    main()
