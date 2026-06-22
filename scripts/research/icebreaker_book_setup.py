"""Order-book DENSITY at each gated setup: does resting liquidity predict the move?

We re-test the book snapshot — this time on the GOOD (robust-gated) setups, not the
naive detector where it was flat. For each setup we reconstruct the resting book near
the level as of the break and measure static density:

  wall_notional  - max resting notional at a single price within tol of the level
  depth_run      - resting notional within `band` on the RUN side (ahead of price)
  depth_base     - resting notional within `band` on the BASE side (behind)
  depth_ratio    - depth_run / depth_base  (thin ahead = easy run = bigger move?)
  book_imbalance - (base - run) / (base + run) within band
  total_depth    - run + base notional (liquidity scale of the coin/level)

Hypothesis (user): if the setup is right, book density shows the MAGNITUDE of the
move. So we report both AUC vs good(MFE>=thr) AND Spearman corr(feature, MFE).

    python scripts/research/icebreaker_book_setup.py \
        --store /root/trading/data/ib_book --cache 'data/ib_gated_FARTCOINUSDT.jsonl' \
        --dump data/ib_book_setup.jsonl
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
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
BID, ASK = 0, 1


def density(surv, L, is_long, t_close, tol, band):
    """Static resting-book density features near L as of t_close."""
    s_ts, s_pr, s_qty, s_side = surv
    run_side = ASK if is_long else BID      # price runs INTO the opposite book
    base_side = BID if is_long else ASK
    out = {}
    for tag, bside in (("run", run_side), ("base", base_side)):
        m = (s_side == bside) & (s_ts <= t_close)
        uniq, last = mc.latest_per_price(s_pr[m], s_qty[m])
        if len(uniq) == 0:
            out[tag] = (0.0, 0.0)
            continue
        notional = uniq * last
        if is_long:
            sel = (uniq >= L * (1 - band)) & (uniq <= L * (1 + band))
        else:
            sel = (uniq >= L * (1 - band)) & (uniq <= L * (1 + band))
        wall = float(notional[np.abs(uniq - L) <= L * tol].max(initial=0.0))
        out[tag] = (float(notional[sel].sum()), wall)
    depth_run, wall_run = out["run"]
    depth_base, wall_base = out["base"]
    tot = depth_run + depth_base
    return {
        "wall_notional": max(wall_run, wall_base),
        "depth_run": depth_run,
        "depth_base": depth_base,
        "depth_ratio": (depth_run / depth_base) if depth_base > 0 else 999.0,
        "book_imbalance": ((depth_base - depth_run) / tot) if tot > 0 else 0.0,
        "total_depth": tot,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--band", type=float, default=0.005)
    p.add_argument("--dump", type=Path, required=True)
    args = p.parse_args()

    recs = []
    for fp in sorted(glob.glob(args.cache)):
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    if args.symbols:
        recs = [r for r in recs if r["symbol"] in args.symbols]
    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    out, no_book = [], 0
    for (sym, date), setups in sorted(by_day.items()):
        levels = [s["level"] for s in setups]
        surv = mc.filter_book(store, sym, date, levels, args.band * 2)
        if len(surv[0]) == 0:
            no_book += len(setups)
            continue
        for s in setups:
            feat = density(surv, s["level"], s["side"] == "long", s["ts_close"],
                           args.tol, args.band)
            out.append({**s, **feat})
        print(f"[{sym} {date}] setups={len(setups)}", flush=True)

    def _native(o):
        if isinstance(o, np.generic):
            return o.item()
        raise TypeError
    with open(args.dump, "w") as f:
        for r in out:
            f.write(json.dumps(r, default=_native) + "\n")
    print(f"\n  book features for {len(out)} setups ({no_book} had no book) -> {args.dump}")


if __name__ == "__main__":
    main()
