"""Balanced eyeball set: render a mix of RUNNER and FAKEOUT setups so a human can look
for a discriminating pattern the features missed. Reuses the proven 2-panel renderer from
icebreaker_chart_setups (price + level + consolidation box + MFE/MAE on top; resting book
at the level over time below), but selects a deliberately BALANCED, coin-spread sample of
good (MFE>=1% runner) and bad (fakeout) setups instead of top-score + random.

    python scripts/research/icebreaker_eyeball.py \
        --store /root/trading/data/icebreaker_active --book-store /root/trading/data/ib_book \
        --cache /root/trading/tmp/ib_liq_empty.jsonl --out /root/trading/tmp/eyeball \
        --n-good 12 --n-bad 12 --bar-ms 60000
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
cs = _load("icebreaker_chart_setups", "scripts/research/icebreaker_chart_setups.py")


def stratified(rows, n, coins):
    """Take ~n rows spread as evenly as possible across coins (shuffled within coin)."""
    by = defaultdict(list)
    for r in rows:
        by[r["symbol"]].append(r)
    for c in by:
        random.shuffle(by[c])
    out, i = [], 0
    pools = [by[c] for c in coins if by[c]]
    while len(out) < n and pools:
        p = pools[i % len(pools)]
        if p:
            out.append(p.pop())
        else:
            pools.remove(p)
            continue
        i += 1
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True, help="tape store for price bars")
    p.add_argument("--book-store", required=True, help="L2 store for the lower book panel")
    p.add_argument("--cache", required=True, help="gated-setup dump w/ ts_close/side/level/mfe/mae/good")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", default=["FARTCOINUSDT", "WIFUSDT", "1000PEPEUSDT"])
    p.add_argument("--n-good", type=int, default=12)
    p.add_argument("--n-bad", type=int, default=12)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--consol-bars", type=int, default=20)
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--band", type=float, default=0.005)
    p.add_argument("--view-before", type=int, default=45)
    p.add_argument("--view-after", type=int, default=45)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    random.seed(args.seed)

    recs = [json.loads(l) for l in open(args.cache) if l.strip()]
    recs = [r for r in recs if r["symbol"] in args.symbols]
    goods = [r for r in recs if r.get("good")]
    bads = [r for r in recs if not r.get("good")]
    chosen = (stratified(goods, args.n_good, args.symbols)
              + stratified(bads, args.n_bad, args.symbols))
    print(f"  pool: {len(goods)} good / {len(bads)} bad over {args.symbols}; "
          f"chose {sum(1 for r in chosen if r.get('good'))} good + "
          f"{sum(1 for r in chosen if not r.get('good'))} bad", flush=True)

    cfg = {"bar_ms": args.bar_ms, "consol_bars": args.consol_bars, "tol": args.tol,
           "band": args.band, "view_before": args.view_before, "view_after": args.view_after}
    store, bstore = Path(args.store), Path(args.book_store)
    by_day = defaultdict(list)
    for r in chosen:
        by_day[(r["symbol"], r["date"])].append(r)

    done = 0
    for (sym, date), setups in sorted(by_day.items()):
        trades = mc.load_trades_arr(store, sym, date)
        t_ts, t_pr, t_qty, _ = trades
        if len(t_ts) == 0:
            print(f"  [{sym} {date}] no tape -- skip {len(setups)}", flush=True)
            continue
        bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)
        surv = mc.filter_book(bstore, sym, date, [s["level"] for s in setups], args.band)
        for r in setups:
            tag = "GOOD" if r.get("good") else "bad_"
            tm = datetime.fromtimestamp(r["ts_close"] / 1000, timezone.utc).strftime("%H%M")
            # prefix mfe so files sort by class then magnitude -> easy side-by-side scan
            name = f"{tag}_mfe{r['mfe']*1000:04.0f}_{sym}_{date}_{tm}_{r['side']}.png"
            if cs.render(bars, surv, trades, r, args.out / name, cfg):
                done += 1
                print(f"  saved {name}", flush=True)
            else:
                print(f"  [skip] {sym} {date} {tm}: bar idx not found (ts_close/bar-ms mismatch)", flush=True)
    print(f"\n{done} charts -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
