"""Breakouts of MAJOR levels — fix the level-detection horizon.

Previous detector used a 1h (12-bar) range and called its edge "the level" -> it
fired on hundreds of intraday micro-consolidations (1049 events), not the few
major S/R breaks a scalper actually trades. Real levels (see the SFP example)
form over many HOURS with multiple touches and break half a day later.

Here a level = a CLUSTER of swing highs/lows over a long lookback (default 24h,
spanning multiple days via continuous bars) touched >= min_touches times, with
the touches spread over >= min_span. A breakout = the close crossing such a level
after price has been hugging it (a base). Far fewer, far higher-quality setups.

    python scripts/research/icebreaker_major.py \
        --store /root/trading/data/icebreaker --symbols TAOUSDT ZECUSDT SUIUSDT \
        --start 2026-03-01 --end 2026-03-31 --dump data/ib_major.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
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


# ----------------------------------------------------------------------------
# Pure detection primitives
# ----------------------------------------------------------------------------
def find_swings(bars, w):
    """Indices of swing highs / lows (STRICT extreme of a ±w window).

    Strict (> / <) so a flat plateau of equal highs is not counted as a run of
    swings (which would fabricate a spurious level)."""
    sh, sl = [], []
    for i in range(w, len(bars) - w):
        hi = max(bars[k]["high"] for k in range(i - w, i + w + 1) if k != i)
        lo = min(bars[k]["low"] for k in range(i - w, i + w + 1) if k != i)
        if bars[i]["high"] > hi:
            sh.append(i)
        if bars[i]["low"] < lo:
            sl.append(i)
    return sh, sl


def cluster_levels(points, tol):
    """Greedy 1-D clustering of (price, idx) by relative tol. Returns (level, members)."""
    if not points:
        return []
    points = sorted(points)
    clusters, cur = [], [points[0]]
    for p in points[1:]:
        ref = sum(x[0] for x in cur) / len(cur)
        if abs(p[0] - ref) / ref <= tol:
            cur.append(p)
        else:
            clusters.append(cur); cur = [p]
    clusters.append(cur)
    return [(sum(x[0] for x in c) / len(c), c) for c in clusters]


def detect_major_breakouts(bars, bar_ms, *, lookback=288, swing_w=3, tol=0.002,
                           min_touches=4, min_span_bars=36, brk=0.0015,
                           cooldown=12, near_bars=24, near_tol=0.004,
                           one_sided=0.70):
    """Breakouts of a CLEAN multi-hour level. One event per bar at most.

    A level qualifies only if, over the lookback, price stayed predominantly on
    ONE side of it (>= one_sided fraction of closes) — i.e. it's a respected
    support/resistance, not a mid-range pivot the price oscillates through. The
    broken side must be the origin side (long breaks resistance from below)."""
    sh, sl = find_swings(bars, swing_w)
    # perf: swings are sorted -> bisect the window slice (not an O(n_swings) filter per
    # bar); closes as a numpy array -> vectorized origin count. Behavior-identical to the
    # prior O(n^2) form, needed for year-long inputs (covered by test_icebreaker_major).
    closes_arr = np.array([b["close"] for b in bars], dtype="float64")
    out, last = [], -10 ** 9
    for i in range(lookback, len(bars)):
        if i - last < cooldown:
            continue
        c, pc, lo = bars[i]["close"], bars[i - 1]["close"], i - lookback
        win = closes_arr[lo:i]
        nwin = i - lo
        fired = False
        for side, swings, pf in (("long", sh, "high"), ("short", sl, "low")):
            lo_i = bisect_left(swings, lo)
            hi_i = bisect_left(swings, i)
            sw = swings[lo_i:hi_i]
            if len(sw) < min_touches:
                continue
            pts = [(bars[j][pf], j) for j in sw]
            for lvl, mem in cluster_levels(pts, tol):
                if len(mem) < min_touches:
                    continue
                span = max(m[1] for m in mem) - min(m[1] for m in mem)
                if span < min_span_bars:
                    continue
                # respected level: price predominantly on the origin side
                origin = int(np.count_nonzero(win < lvl if side == "long" else win > lvl))
                if origin / nwin < one_sided:
                    continue
                broke = ((side == "long" and pc <= lvl and c > lvl * (1 + brk)) or
                         (side == "short" and pc >= lvl and c < lvl * (1 - brk)))
                if not broke:
                    continue
                recent = bars[max(0, i - near_bars):i]
                if min(abs(b["close"] - lvl) / lvl for b in recent) > near_tol:
                    continue   # price wasn't hugging the level -> not a base break
                out.append({"ts_close": bars[i]["ts"] + bar_ms, "side": side,
                            "level": lvl, "touches": len(mem), "span_bars": int(span),
                            "lookback_bars": lookback, "one_sided": round(origin / nwin, 2)})
                last, fired = i, True
                break
            if fired:
                break
    return out


# ----------------------------------------------------------------------------
# Runner: continuous bars per coin, MFE from the tape
# ----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000, help="bar size (1m default)")
    p.add_argument("--lookback-bars", type=int, default=480, help="level horizon (8h@1m)")
    p.add_argument("--swing-w", type=int, default=5, help="pivot half-window")
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--min-touches", type=int, default=4)
    p.add_argument("--min-span-bars", type=int, default=120, help="touches span >= 2h@1m")
    p.add_argument("--brk", type=float, default=0.0015)
    p.add_argument("--cooldown", type=int, default=30)
    p.add_argument("--near-bars", type=int, default=30)
    p.add_argument("--near-tol", type=float, default=0.003)
    p.add_argument("--one-sided", type=float, default=0.70, help="min frac of closes on origin side")
    p.add_argument("--horizon-ms", type=int, default=900_000)
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--dump", type=Path, required=True)
    args = p.parse_args()

    store = Path(args.store)
    recs = []
    for sym in args.symbols:
        # continuous bars across the whole range (levels span days)
        bars, tape_by_date = [], {}
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            t_ts, t_pr, t_qty, _ = mc.load_trades_arr(store, sym, date)
            if len(t_ts) == 0:
                continue
            bars.extend(mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms))
            tape_by_date[date] = (t_ts, t_pr)
        bks = detect_major_breakouts(
            bars, args.bar_ms, lookback=args.lookback_bars, swing_w=args.swing_w,
            tol=args.tol, min_touches=args.min_touches,
            min_span_bars=args.min_span_bars, brk=args.brk, cooldown=args.cooldown,
            near_bars=args.near_bars, near_tol=args.near_tol, one_sided=args.one_sided)
        kept = 0
        for b in bks:
            date = datetime.fromtimestamp(b["ts_close"] / 1000, timezone.utc).strftime("%Y-%m-%d")
            tp = tape_by_date.get(date)
            if tp is None:
                continue
            mv = bs.measure_move(tp[0], tp[1], b["ts_close"], b["side"], args.horizon_ms)
            if mv is None:
                continue
            entry, mfe, mae, t_peak = mv
            b.update(entry=entry, mfe=mfe, mae=mae, t_peak_ms=t_peak, symbol=sym,
                     date=date, good=int(mfe >= args.good_mfe))
            recs.append(b); kept += 1
        g = sum(r["good"] for r in recs if r["symbol"] == sym)
        print(f"[{sym}] major-breakouts={kept} good={g}", flush=True)

    def _native(o):
        if isinstance(o, np.generic):
            return o.item()
        raise TypeError
    with open(args.dump, "w") as f:
        for r in recs:
            f.write(json.dumps(r, default=_native) + "\n")
    n = len(recs)
    g = sum(r["good"] for r in recs)
    print("\n" + "=" * 60)
    print(f"  MAJOR-LEVEL BREAKOUTS  n={n}  good(MFE>={args.good_mfe:.1%})={g} "
          f"({g/n:.1%})" if n else "  none")
    if n:
        mfes = sorted(r["mfe"] for r in recs)
        print(f"  MFE median={mfes[n//2]:.2%} p90={mfes[int(n*.9)]:.2%} max={mfes[-1]:.2%}")
        print(f"  dumped {n} -> {args.dump}")


if __name__ == "__main__":
    main()
