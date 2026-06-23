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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--fee", type=float, default=0.00055)
    p.add_argument("--tag", default="")
    args = p.parse_args()

    recs = [json.loads(l) for l in open(args.cache) if l.strip()]
    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    close_rows, level_rows = [], []
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

    print(f"\n################  ENTRY = CLOSE (today's model)  —  {args.tag}  ################")
    pd.decomp(f"{args.tag} close", close_rows, args.fee)
    print(f"\n################  ENTRY = LEVEL (ideal fill, ignores fill-risk)  —  {args.tag}  ################")
    pd.decomp(f"{args.tag} level", level_rows, args.fee)


if __name__ == "__main__":
    main()
