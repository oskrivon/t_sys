"""Realistic scalper exit on breakout setups — realized PnL, not AUC.

The classification test was the wrong question: a scalper doesn't predict which
break runs, they manage R — tiny stop at the invalidation (back below the level),
scale out, trail the rest. This walks the trade tape per setup and simulates:

  entry  = first trade at/after the break
  stop   = just behind the broken level (precise invalidation)
  TP1    = take f1 of size at +tp1, then move stop to breakeven
  trail  = give back `trail_giveback` from the peak on the remainder
  exit   = stop / trail / horizon (MTM)

Reports realized expectancy + R-distribution across structural-selection tiers
(all / top-25% / top-decile by width+brk_strength−touches), per coin, at taker
and maker fees. This is the decisive test of "is there a tradeable edge in the
structural setups" — the most likely reason scalpers profit where our AUC was flat.

    python scripts/research/icebreaker_exit_sim.py \
        --store /root/trading/data/icebreaker --cache data/ib_micro.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
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


# ----------------------------------------------------------------------------
# Pure exit simulator (testable)
# ----------------------------------------------------------------------------
def simulate_exit(ts, px, ts0, side, level, cfg):
    """Walk the tape; return dict(gross, fee_units, risk, R, reason, mfe).

    gross = pre-fee realized return; net = gross - fee_side*fee_units.
    R is net(at taker)/risk. Returns None if no trade after ts0.
    """
    i = bisect_left(ts, ts0)
    if i >= len(ts):
        return None
    entry = float(px[i])
    dirn = 1.0 if side == "long" else -1.0
    stop0 = level * (1 - cfg["buffer"]) if side == "long" else level * (1 + cfg["buffer"])
    risk = dirn * (entry - stop0) / entry          # fractional risk to invalidation
    tp1_px = entry * (1 + dirn * cfg["tp1"])
    gb, f1 = cfg["trail_giveback"], cfg["f1"]
    t_end = ts0 + cfg["horizon_ms"]

    legs = []                # (fraction, exit_price)
    remaining, tp1_done = 1.0, False
    best = entry
    reason = "horizon"
    j, last = i, entry
    while j < len(ts) and ts[j] <= t_end:
        p = float(px[j]); last = p
        if dirn * (p - best) > 0:
            best = p
        if tp1_done:
            trail = best * (1 - dirn * gb)
            eff = max(trail, entry) if side == "long" else min(trail, entry)
            if dirn * (p - eff) <= 0:
                legs.append((remaining, eff)); remaining = 0.0; reason = "trail"; break
        else:
            if dirn * (p - stop0) <= 0:
                legs.append((remaining, stop0)); remaining = 0.0; reason = "stop"; break
            if dirn * (p - tp1_px) >= 0:
                legs.append((f1, tp1_px)); remaining -= f1; tp1_done = True; best = p
        j += 1
    if remaining > 1e-9:
        legs.append((remaining, last))

    gross = sum(g * dirn * (pp - entry) / entry for g, pp in legs)
    fee_units = 1.0 + sum(g for g, _ in legs)       # entry + every exit leg
    net_taker = gross - cfg["fee_side"] * fee_units
    return {"gross": gross, "fee_units": fee_units, "risk": risk,
            "R": (net_taker / risk) if risk > 1e-9 else float("nan"),
            "reason": reason, "mfe": dirn * (best - entry) / entry}


# ----------------------------------------------------------------------------
# Selection + reporting
# ----------------------------------------------------------------------------
def within_coin_score(recs):
    """width↑ + brk_strength↑ + (few touches)↑, ranked within each coin."""
    for sym in set(r["symbol"] for r in recs):
        s = [r for r in recs if r["symbol"] == sym]
        n = len(s)
        for f, rev in (("width", False), ("brk_strength", False), ("touches", True)):
            order = sorted(s, key=lambda r: r[f], reverse=rev)
            for i, r in enumerate(order):
                r.setdefault("_pct", {})[f] = i / n
    for r in recs:
        p = r["_pct"]
        r["score"] = p["width"] + p["brk_strength"] + (1 - p["touches"])


def summarize(name, rows, fee_side):
    n = len(rows)
    if not n:
        print(f"  {name:22s} n=   0")
        return
    nets = [r["gross"] - fee_side * r["fee_units"] for r in rows]
    wins = sum(1 for x in nets if x > 0)
    mean = sum(nets) / n
    med = sorted(nets)[n // 2]
    Rs = [r["R"] for r in rows if r["R"] == r["R"]]
    meanR = sum(Rs) / len(Rs) if Rs else float("nan")
    reasons = Counter(r["reason"] for r in rows)
    print(f"  {name:22s} n={n:4d}  win={wins/n:5.1%}  mean={mean*100:+.3f}%  "
          f"med={med*100:+.3f}%  meanR={meanR:+.2f}  sum={sum(nets)*100:+.1f}%  "
          f"[{dict(reasons)}]")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--buffer", type=float, default=0.001, help="stop behind level")
    p.add_argument("--tp1", type=float, default=0.005)
    p.add_argument("--f1", type=float, default=0.5, help="fraction taken at TP1")
    p.add_argument("--trail-giveback", type=float, default=0.005)
    p.add_argument("--horizon-ms", type=int, default=900_000)
    p.add_argument("--fee-taker", type=float, default=0.00055, help="per-side taker")
    p.add_argument("--fee-maker", type=float, default=0.0002, help="per-side maker")
    args = p.parse_args()

    cfg = {"buffer": args.buffer, "tp1": args.tp1, "f1": args.f1,
           "trail_giveback": args.trail_giveback, "horizon_ms": args.horizon_ms,
           "fee_side": args.fee_taker}

    recs = [json.loads(l) for l in open(args.cache) if l.strip()]
    has_struct = bool(recs) and "width" in recs[0]
    has_score = bool(recs) and "score" in recs[0]
    if has_struct:
        within_coin_score(recs)
    elif has_score:             # robust dump: rank by the precomputed quality score
        pass
    else:                       # major dump: no tiers
        for r in recs:
            r["score"] = 0.0
    has_tiers = has_struct or has_score

    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    sim = []
    for (sym, date), setups in sorted(by_day.items()):
        t_ts, t_pr, _, _ = mc.load_trades_arr(store, sym, date)
        if len(t_ts) == 0:
            continue
        for r in setups:
            res = simulate_exit(t_ts, t_pr, r["ts_close"], r["side"], r["level"], cfg)
            if res:
                sim.append({**r, **res})

    n = len(sim)
    sim.sort(key=lambda r: r["score"], reverse=True)
    top25 = sim[:n // 4]
    top10 = sim[:n // 10]

    for tag, fee in (("TAKER", args.fee_taker), ("MAKER", args.fee_maker)):
        print("\n" + "=" * 92)
        print(f"  SCALPER EXIT  ({tag} {fee*2:.2%} round-trip)  stop@level-{args.buffer:.1%}, "
              f"TP1 {args.f1:.0%}@+{args.tp1:.1%}->BE, trail {args.trail_giveback:.1%}, "
              f"hold {args.horizon_ms//60000}m")
        print("=" * 92)
        summarize("ALL", sim, fee)
        if has_tiers:
            summarize("top-25% score", top25, fee)
            summarize("top-decile score", top10, fee)
        per = top10 if has_tiers else sim
        for sym in sorted(set(r["symbol"] for r in per)):
            summarize(f"  {sym}", [r for r in per if r["symbol"] == sym], fee)


if __name__ == "__main__":
    main()
