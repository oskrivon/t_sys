"""Base-rate check: does level QUALITY predict the move, or are we polishing noise?

Same tape, same payoff measure (measure_move, same horizon + MFE threshold). Only
the SELECTION differs:

  B0 random   - N random (ts, side) entries -> floor from raw volatility alone.
  B1 ungated  - detect_setups with every quality gate OFF (any decisive break of
                a >=min_touches pivot level near price) -> "a breakout happened".
  B2 gated    - detect_setups with the tuned gates (shelf/air/edge/pocD).

If B2 good-rate ~= B0/B1, the features carry no signal and ML/Vision won't help.
If B2 >> B0, the quality features carry signal worth learning.

    python scripts/research/icebreaker_baserate.py \
        --store /root/trading/data/icebreaker_active --symbols DOGEUSDT WIFUSDT ... \
        --start 2026-03-01 --end 2026-03-31 --dump data/ib_baserate.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
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
bs = _load("icebreaker_breakout_study", "scripts/research/icebreaker_breakout_study.py")
from src.strategy import robust_levels as rl


def _measure(tape_by_date, bar_ms, idx, side, bars, horizon_ms):
    """measure_move for a bar index, resolving the tape by the close date."""
    ts_close = bars[idx]["ts"] + bar_ms
    date = datetime.fromtimestamp(ts_close / 1000, timezone.utc).strftime("%Y-%m-%d")
    tp = tape_by_date.get(date)
    if tp is None:
        return None
    return bs.measure_move(tp[0], tp[1], ts_close, side, horizon_ms)


def _summary(name, mfes, good_mfe):
    n = len(mfes)
    if not n:
        return f"  {name:10s} n=0"
    mfes = sorted(mfes)
    g = sum(1 for m in mfes if m >= good_mfe)
    return (f"  {name:10s} n={n:5d}  good(>={good_mfe:.0%})={g/n:6.1%}  "
            f"MFEmed={mfes[n // 2]:.2%}  p90={mfes[int(n * .9)]:.2%}  max={mfes[-1]:.2%}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--lookback", type=int, default=480)
    p.add_argument("--horizon-ms", type=int, default=900_000)
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--n-random", type=int, default=3000, help="random entries per symbol")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--dump", type=Path, required=True)
    args = p.parse_args()

    store = Path(args.store)
    rng = np.random.RandomState(args.seed)
    agg = {"B0_random": [], "B1_ungated": [], "B2_gated": []}
    recs = []

    for sym in args.symbols:
        bars, tape_by_date = [], {}
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            t_ts, t_pr, t_qty, _ = mc.load_trades_arr(store, sym, date)
            if len(t_ts) == 0:
                continue
            bars.extend(mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms))
            tape_by_date[date] = (t_ts, t_pr)
        if len(bars) <= args.lookback + 5:
            print(f"[{sym}] too few bars", flush=True)
            continue

        # B0 random (ts, side)
        b0 = []
        idxs = rng.randint(args.lookback, len(bars), size=args.n_random)
        sides = rng.randint(0, 2, size=args.n_random)
        for k in range(args.n_random):
            side = "long" if sides[k] else "short"
            mv = _measure(tape_by_date, args.bar_ms, int(idxs[k]), side, bars, args.horizon_ms)
            if mv is not None:
                b0.append(mv[1])
        agg["B0_random"].extend(b0)

        # B1 ungated: every decisive break of a touched level near price
        ungated = rl.detect_setups(
            bars, lookback=args.lookback, require_squeeze=False,
            one_sided_min=0.0, air_max=1e9, edge_band=1.0,
            min_vol_at_level=0.0, min_poc_dist_atr=0.0, min_score=0.0)
        b1 = []
        for s in ungated:
            mv = _measure(tape_by_date, args.bar_ms, s.idx, s.side, bars, args.horizon_ms)
            if mv is not None:
                b1.append(mv[1])
        agg["B1_ungated"].extend(b1)

        # B2 gated: tuned detector (defaults)
        gated = rl.detect_setups(bars, lookback=args.lookback, require_squeeze=True)
        b2 = []
        for s in gated:
            mv = _measure(tape_by_date, args.bar_ms, s.idx, s.side, bars, args.horizon_ms)
            if mv is None:
                continue
            b2.append(mv[1])
            recs.append({"symbol": sym, "side": s.side, "level": s.level,
                         "mfe": mv[1], "mae": mv[2], "good": int(mv[1] >= args.good_mfe),
                         "score": s.score, "one_sided": s.one_sided, **s.detail})
        agg["B2_gated"].extend(b2)
        print(f"[{sym}] random={len(b0)} ungated={len(b1)} gated={len(b2)}", flush=True)

    with open(args.dump, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

    print("\n" + "=" * 70)
    print(f"  BASE-RATE  horizon={args.horizon_ms // 60000}m  good=MFE>={args.good_mfe:.0%}")
    for name in ("B0_random", "B1_ungated", "B2_gated"):
        print(_summary(name, agg[name], args.good_mfe))
    print(f"  dumped {len(recs)} gated setups -> {args.dump}")
    # lift: B2 good-rate / B0 good-rate
    def gr(v):
        return sum(1 for m in v if m >= args.good_mfe) / len(v) if v else 0.0
    g0, g2 = gr(agg["B0_random"]), gr(agg["B2_gated"])
    if g0 > 0:
        print(f"  LIFT  B2/B0 = {g2 / g0:.2f}x   (>1 = quality adds signal)")


if __name__ == "__main__":
    main()
