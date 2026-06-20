"""Breakout characterization: what separates GOOD moves from fakeouts?

New framing (icebreaker): a manual scalper enters on a clean break of a
consolidation, scales out, and rides a big move. We don't care about average
fixed-TP edge — we care about MFE (how far it ran). This script:

  1. Detects consolidation-breakout events from 5m bars (structure-first; does
     NOT require a $100k wall — the wall/book is a *discriminator*, not the gate).
  2. Measures the realized move from the trade tape over a horizon (default 15m):
     MFE (max favorable excursion = the scalper's payoff), MAE, time-to-peak.
  3. Optionally (--with-book) replays the L2 book to snapshot, at the break
     instant, the order-book signature: wall at the level, supply/demand to chew
     through, top-of-book imbalance, spread. This is the user's "смотрим стакан".
  4. Dumps one fat feature record per breakout -> fed to icebreaker_breakout_analyze.py
     which asks: which features separate MFE>=1% movers from the rest?

Cheap pass (structure + tape only):
    python scripts/research/icebreaker_breakout_study.py \
        --store /root/trading/data/icebreaker --symbols TAOUSDT ZECUSDT SUIUSDT \
        --start 2026-03-01 --end 2026-03-31 --dump data/ib_breakouts.jsonl

Heavy pass (+ order-book features at the break):
    ... --with-book --dump data/ib_breakouts_book.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


sbt = _load("icebreaker_signal_backtest", "scripts/research/icebreaker_signal_backtest.py")
stb = _load("icebreaker_structure_backtest", "scripts/research/icebreaker_structure_backtest.py")
from src.icebreaker.book import BookState


# ----------------------------------------------------------------------------
# Breakout detection from bars
# ----------------------------------------------------------------------------
def detect_breakouts(bars, bar_ms, *, consol_bars, range_thresh, brk, tol,
                     min_touches, cooldown):
    """Find clean breakouts of a prior consolidation range.

    A bar breaks out when, after `consol_bars` of range <= range_thresh, its
    close clears the range edge by `brk`. The broken edge must have been touched
    >= min_touches times (a real level). `cooldown` bars suppress re-counting the
    same move. Returns dicts with the breakout context + pre-trade features.
    """
    out = []
    last_fire = -10 ** 9
    for i in range(consol_bars, len(bars)):
        if i - last_fire < cooldown:
            continue
        win = bars[i - consol_bars:i]
        hi = max(b["high"] for b in win)
        lo = min(b["low"] for b in win)
        mid = (hi + lo) / 2
        if mid <= 0:
            continue
        width = (hi - lo) / mid
        if width > range_thresh:
            continue
        c = bars[i]["close"]
        if c > hi * (1 + brk):
            side, level, ref = "long", hi, "high"
        elif c < lo * (1 - brk):
            side, level, ref = "short", lo, "low"
        else:
            continue
        touches = sum(1 for b in win if abs(b[ref] - level) / level <= tol)
        if touches < min_touches:
            continue
        h = consol_bars // 2
        early = max(b["high"] for b in win[:h]) - min(b["low"] for b in win[:h])
        recent = max(b["high"] for b in win[h:]) - min(b["low"] for b in win[h:])
        squeeze = int(early > 0 and recent <= 0.7 * early)
        vol_consol = sum(b["volume"] for b in win) / consol_bars
        out.append({
            "ts_close": bars[i]["ts"] + bar_ms, "side": side, "level": level,
            "width": width, "touches": touches, "squeeze": squeeze,
            "vol_burst": (bars[i]["volume"] / vol_consol) if vol_consol > 0 else 0.0,
            "brk_strength": abs(c - level) / level,
        })
        last_fire = i
    return out


# ----------------------------------------------------------------------------
# Realized move from the trade tape
# ----------------------------------------------------------------------------
def measure_move(ts_sorted, px_sorted, ts0, side, horizon_ms):
    """Return (entry, mfe, mae, t_peak_ms) over [ts0, ts0+horizon] or None."""
    i = bisect_left(ts_sorted, ts0)
    if i >= len(ts_sorted):
        return None
    entry = px_sorted[i]
    t_end = ts0 + horizon_ms
    hi = lo = entry
    t_hi = t_lo = ts_sorted[i]
    j = i
    while j < len(ts_sorted) and ts_sorted[j] <= t_end:
        p = px_sorted[j]
        if p > hi:
            hi, t_hi = p, ts_sorted[j]
        if p < lo:
            lo, t_lo = p, ts_sorted[j]
        j += 1
    if side == "long":
        return entry, (hi - entry) / entry, (entry - lo) / entry, t_hi - ts0
    return entry, (entry - lo) / entry, (hi - entry) / entry, t_lo - ts0


# ----------------------------------------------------------------------------
# Order-book features at the break instant (optional, heavy)
# ----------------------------------------------------------------------------
def book_features_at(book, level, side, tol, band):
    """Snapshot the book signature relevant to the break.

    Long breaks through resistance -> the barrier is ASKS at/above the level;
    `through_depth` is the supply that must be eaten to run. Symmetric for short.
    """
    if side == "long":
        wall = max((p * s for p, s in book.asks.items()
                    if abs(p - level) / level <= tol), default=0.0)
        through = sum(p * s for p, s in book.asks.items()
                      if level <= p <= level * (1 + band))
    else:
        wall = max((p * s for p, s in book.bids.items()
                    if abs(p - level) / level <= tol), default=0.0)
        through = sum(p * s for p, s in book.bids.items()
                      if level * (1 - band) <= p <= level)
    bid_d = sum(p * s for p, s in book.bids.items())
    ask_d = sum(p * s for p, s in book.asks.items())
    imb = (bid_d - ask_d) / (bid_d + ask_d) if (bid_d + ask_d) > 0 else 0.0
    mid = book.mid or level
    spread = book.spread or 0.0
    return {"wall_notional": wall, "through_depth": through, "imbalance": imb,
            "spread_bps": (spread / mid * 1e4) if mid else 0.0}


def attach_book_features(store, symbol, date, breakouts, tol, band):
    """Replay the book once; snapshot features at each breakout's ts_close."""
    bos = sorted(breakouts, key=lambda b: b["ts_close"])
    book = BookState()
    cur_snap = None
    idx = 0
    for ts, uid, kind, side, price, qty in sbt.iter_book_diffs(store, symbol, date):
        if kind == "snapshot" and uid != cur_snap:
            book.bids.clear(); book.asks.clear(); cur_snap = uid; book.seeded = True
        book._apply_level(book.bids if side == "bid" else book.asks, price, qty)
        while idx < len(bos) and bos[idx]["ts_close"] <= ts:
            bos[idx].update(book_features_at(book, bos[idx]["level"],
                                             bos[idx]["side"], tol, band))
            idx += 1
    # breakouts after the last diff: no book -> leave book keys absent
    return breakouts


def load_tape(store, symbol, date):
    rows = stb.load_bar_trades(store, symbol, date)  # already ts-sorted
    return [r[0] for r in rows], [r[1] for r in rows]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=300_000)
    p.add_argument("--consol-bars", type=int, default=12)
    p.add_argument("--range-thresh", type=float, default=0.025)
    p.add_argument("--brk", type=float, default=0.001, help="close beyond edge to confirm")
    p.add_argument("--tol", type=float, default=0.0015, help="level touch tolerance")
    p.add_argument("--min-touches", type=int, default=2)
    p.add_argument("--cooldown", type=int, default=6, help="bars to suppress re-fire")
    p.add_argument("--horizon-ms", type=int, default=900_000, help="MFE window (15m)")
    p.add_argument("--band", type=float, default=0.005, help="through-depth band beyond level")
    p.add_argument("--good-mfe", type=float, default=0.01, help="MFE>=this = good move")
    p.add_argument("--with-book", action="store_true", help="add L2 features (heavy)")
    p.add_argument("--dump", type=Path, required=True)
    args = p.parse_args()

    recs = []
    n_good = 0
    for symbol in args.symbols:
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(args.store, symbol, date, "trades"):
                continue
            bars = stb.build_bars(args.store, symbol, date, args.bar_ms)
            bks = detect_breakouts(
                bars, args.bar_ms, consol_bars=args.consol_bars,
                range_thresh=args.range_thresh, brk=args.brk, tol=args.tol,
                min_touches=args.min_touches, cooldown=args.cooldown)
            if not bks:
                print(f"[{symbol} {date}] breakouts=0")
                continue
            ts_s, px_s = load_tape(args.store, symbol, date)
            kept = []
            for b in bks:
                mv = measure_move(ts_s, px_s, b["ts_close"], b["side"], args.horizon_ms)
                if mv is None:
                    continue
                entry, mfe, mae, t_peak = mv
                b.update(entry=entry, mfe=mfe, mae=mae, t_peak_ms=t_peak,
                         symbol=symbol, date=date,
                         good=int(mfe >= args.good_mfe))
                kept.append(b)
            if args.with_book and kept:
                attach_book_features(args.store, symbol, date, kept, args.tol, args.band)
            recs.extend(kept)
            g = sum(x["good"] for x in kept)
            n_good += g
            print(f"[{symbol} {date}] breakouts={len(kept)} good={g}")

    with open(args.dump, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

    n = len(recs)
    print("\n" + "=" * 64)
    print(f"  BREAKOUT STUDY  n={n}  good(MFE>={args.good_mfe:.1%})={n_good} "
          f"({n_good/n:.1%})" if n else "  no breakouts")
    if n:
        mfes = sorted(r["mfe"] for r in recs)
        print(f"  MFE  median={mfes[n//2]:.2%}  p75={mfes[int(n*.75)]:.2%}  "
              f"p90={mfes[int(n*.9)]:.2%}  max={mfes[-1]:.2%}")
        print(f"  dumped {n} breakouts -> {args.dump}")


if __name__ == "__main__":
    main()
