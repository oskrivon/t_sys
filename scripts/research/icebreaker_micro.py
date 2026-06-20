"""Dynamic order-book micro-features for breakouts — fast, targeted, parallel.

Why this exists: reconstructing the full L2 book in a Python row-loop costs ~293s
per coin-day (27M deltas) while merely *reading* that parquet is 2.1s. We only
need a handful of price levels (the breakout levels), so we vectorized-filter the
delta stream to rows whose price is within a band of a breakout level (numpy mask,
~6s/day) and compute per-level DYNAMIC features on the survivors — the time
evolution a discretionary trader actually reads:

  - resting-size timeline at the level (the wall over time)
  - eaten (trades through the level, break direction) vs cancelled (size removed
    without a trade)  ->  cancel:eat ratio  (our prior finding: walls are pulled
    ~800:1 vs eaten — this measures it per setup)
  - absorbed_frac (eaten / wall peak), absorption_rate (eaten/sec)
  - refills (size recovers after a >50% drop = iceberg signature)
  - tape aggression slope into the level (accelerating or fading)
  - static wall_notional / through_depth at the break (free from survivors)

These feed icebreaker_breakout_analyze.py: do dynamic features separate MFE>=1%
movers from fakeouts (AUC >> 0.5) where the static snapshot did not?

    python scripts/research/icebreaker_micro.py \
        --store /root/trading/data/icebreaker --symbols TAOUSDT ZECUSDT SUIUSDT \
        --start 2026-03-01 --end 2026-03-31 --workers 3 --dump data/ib_micro.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

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
bs = _load("icebreaker_breakout_study", "scripts/research/icebreaker_breakout_study.py")

BID, ASK = 0, 1
SELL, BUY = 0, 1


# ----------------------------------------------------------------------------
# Pure, testable primitives
# ----------------------------------------------------------------------------
def band_mask(prices, levels, band):
    """Bool mask: price within ±band (relative) of ANY level. Vectorized."""
    mask = np.zeros(len(prices), dtype=bool)
    for L in levels:
        mask |= (prices >= L * (1 - band)) & (prices <= L * (1 + band))
    return mask


def latest_per_price(price, qty):
    """Latest (absolute) size per price, given ts-ascending input. Vectorized."""
    if len(price) == 0:
        return np.empty(0), np.empty(0)
    pr = np.round(price, 8)
    order = np.argsort(pr, kind="stable")          # group by price, keep ts order
    pr_s, qty_s = pr[order], qty[order]
    uniq, start = np.unique(pr_s, return_index=True)
    end = np.append(start[1:], len(pr_s)) - 1       # last row of each price group
    return uniq, qty_s[end]


def resting_dynamics(price, qty, L, tol):
    """From ts-ascending level updates, return (max_wall_notional, removed_notional,
    refills) where removed = sum of size decreases and refill = recovery after a
    >50% drop (iceberg tell)."""
    pr = np.round(price, 8)
    sel = np.abs(pr - L) <= L * tol
    pr, qty = pr[sel], qty[sel]
    max_wall = removed = 0.0
    refills = 0
    for p in np.unique(pr):
        seq = qty[pr == p]
        if len(seq) == 0:
            continue
        max_wall = max(max_wall, float(seq.max()) * p)
        d = np.diff(seq)
        removed += float(-d[d < 0].sum()) * p
        peak = 0.0
        dropped = False
        for v in seq:
            if peak > 0 and v >= 0.9 * peak and dropped:
                refills += 1
                dropped = False
            if peak > 0 and v <= 0.5 * peak:
                dropped = True
            peak = max(peak, float(v))
    return max_wall, removed, refills


def static_book(price, qty, side, ts, L, is_long, t_close, tol, band):
    """Wall notional at the level + supply/demand to chew through, as of t_close."""
    bside = ASK if is_long else BID
    m = (side == bside) & (ts <= t_close)
    uniq, last = latest_per_price(price[m], qty[m])
    if len(uniq) == 0:
        return 0.0, 0.0
    notional = uniq * last
    wall = float(notional[np.abs(uniq - L) <= L * tol].max(initial=0.0))
    if is_long:
        through = float(notional[(uniq >= L) & (uniq <= L * (1 + band))].sum())
    else:
        through = float(notional[(uniq >= L * (1 - band)) & (uniq <= L)].sum())
    return wall, through


def dyn_features(surv, trades, b, cfg):
    """Compute the dynamic + static book features for one breakout."""
    s_ts, s_pr, s_qty, s_side = surv
    t_ts, t_pr, t_qty, t_side = trades
    L = b["level"]
    is_long = b["side"] == "long"
    t1 = b["ts_close"]
    t0 = t1 - cfg["window_ms"]
    tol, band = cfg["tol"], cfg["band"]
    bside = ASK if is_long else BID
    eat = BUY if is_long else SELL

    # resting-size dynamics at the level over the pre-break window
    m = (s_side == bside) & (s_ts >= t0) & (s_ts <= t1)
    o = np.argsort(s_ts[m], kind="stable")
    lp, lq = s_pr[m][o], s_qty[m][o]
    max_wall, removed, refills = resting_dynamics(lp, lq, L, tol)

    # eaten = aggressor trades through the level in the window
    tm = (t_side == eat) & (t_ts >= t0) & (t_ts <= t1) & (np.abs(t_pr - L) <= L * tol)
    eaten = float((t_pr[tm] * t_qty[tm]).sum())
    cancelled = max(removed - eaten, 0.0)

    half = (t0 + t1) / 2
    fh = float((t_pr[tm] * t_qty[tm])[t_ts[tm] < half].sum())
    sh = float((t_pr[tm] * t_qty[tm])[t_ts[tm] >= half].sum())

    wall, through = static_book(s_pr, s_qty, s_side, s_ts, L, is_long, t1, tol, band)
    return {
        "max_wall": max_wall,
        "eaten": eaten,
        "cancelled": cancelled,
        "cancel_eat": (cancelled / eaten) if eaten > 0 else (999.0 if removed > 0 else 0.0),
        "absorbed_frac": (eaten / max_wall) if max_wall > 0 else 0.0,
        "absorption_rate": eaten / (cfg["window_ms"] / 1000.0),
        "refills": refills,
        "aggr_slope": (sh / fh) if fh > 0 else (1.0 if sh == 0 else 9.99),
        "wall_notional": wall,
        "through_depth": through,
    }


# ----------------------------------------------------------------------------
# I/O per coin-day
# ----------------------------------------------------------------------------
def _np(col):
    return col.to_numpy(zero_copy_only=False)


def bars_np(ts, price, qty, bar_ms):
    """Vectorized OHLCV bars from ts-ascending trades (loop over ~buckets, not rows)."""
    if len(ts) == 0:
        return []
    bucket = ts // bar_ms
    uniq, start = np.unique(bucket, return_index=True)   # ts sorted -> bucket non-decr
    end = np.append(start[1:], len(ts))
    bars = []
    for k in range(len(uniq)):
        s, e = int(start[k]), int(end[k])
        bars.append({"ts": int(uniq[k]) * bar_ms, "open": float(price[s]),
                     "high": float(price[s:e].max()), "low": float(price[s:e].min()),
                     "close": float(price[e - 1]), "volume": float(qty[s:e].sum())})
    return bars


def load_trades_arr(store, symbol, date):
    """ts-sorted (ts, price, qty, side-int8 buy=1/sell=0). Vectorized via arrow compute."""
    ts, pr, qty, sd = [], [], [], []
    for f in sbt._parts(store, symbol, date, "trades"):
        t = pq.read_table(f, columns=["exch_ts_ms", "price", "qty", "side"])
        ts.append(_np(t.column("exch_ts_ms")))
        pr.append(_np(t.column("price")))
        qty.append(_np(t.column("qty")))
        sd.append(pc.equal(t.column("side"), "buy").to_numpy(zero_copy_only=False))
    if not ts:
        z = np.empty(0)
        return (z.astype("int64"), z, z, z.astype("int8"))
    ts = np.concatenate(ts); pr = np.concatenate(pr); qty = np.concatenate(qty)
    side = np.concatenate(sd).astype("int8")
    o = np.argsort(ts, kind="stable")
    return (ts[o], pr[o], qty[o], side[o])


def filter_book(store, symbol, date, levels, band):
    """Survivors (ts, price, qty, side-int8 ask=1/bid=0) within ±band of any level.

    Side conversion is vectorized (arrow compute) and applied only to survivors —
    never a Python loop over the full 27M-row stream.
    """
    ts, pr, qty, sd = [], [], [], []
    lv = np.array(levels, dtype="float64")
    for f in sbt._parts(store, symbol, date, "book_diff"):
        d = pq.read_table(f, columns=["exch_ts_ms", "price", "qty", "side"])
        p = _np(d.column("price"))
        mask = band_mask(p, lv, band)
        if not mask.any():
            continue
        idx = np.nonzero(mask)[0]
        is_ask = pc.equal(d.column("side"), "ask").to_numpy(zero_copy_only=False)
        ts.append(_np(d.column("exch_ts_ms"))[idx])
        pr.append(p[idx])
        qty.append(_np(d.column("qty"))[idx])
        sd.append(is_ask[idx].astype("int8"))   # ask=1=ASK, bid=0=BID
    if not ts:
        z = np.empty(0)
        return (z.astype("int64"), z, z, z.astype("int8"))
    return (np.concatenate(ts), np.concatenate(pr),
            np.concatenate(qty), np.concatenate(sd))


def process_day(job):
    store, symbol, date, cfg = job
    store = Path(store)
    if not sbt._parts(store, symbol, date, "trades"):
        return []
    trades = load_trades_arr(store, symbol, date)
    t_ts, t_pr, t_qty, _ = trades
    bars = bars_np(t_ts, t_pr, t_qty, cfg["bar_ms"])
    bks = bs.detect_breakouts(
        bars, cfg["bar_ms"], consol_bars=cfg["consol_bars"],
        range_thresh=cfg["range_thresh"], brk=cfg["brk"], tol=cfg["level_tol"],
        min_touches=cfg["min_touches"], cooldown=cfg["cooldown"])
    if not bks:
        print(f"[{symbol} {date}] breakouts=0", flush=True)
        return []
    surv = filter_book(store, symbol, date, [b["level"] for b in bks], cfg["band"])
    out = []
    for b in bks:
        mv = bs.measure_move(t_ts, t_pr, b["ts_close"], b["side"], cfg["horizon_ms"])
        if mv is None:
            continue
        entry, mfe, mae, t_peak = mv
        b.update(entry=entry, mfe=mfe, mae=mae, t_peak_ms=t_peak,
                 symbol=symbol, date=date, good=int(mfe >= cfg["good_mfe"]))
        b.update(dyn_features(surv, trades, b, cfg))
        out.append(b)
    print(f"[{symbol} {date}] breakouts={len(out)} good={sum(x['good'] for x in out)}",
          flush=True)
    return out


def _init_worker():
    try:
        os.nice(10)   # be gentle on the live trading box
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--bar-ms", type=int, default=300_000)
    p.add_argument("--consol-bars", type=int, default=12)
    p.add_argument("--range-thresh", type=float, default=0.025)
    p.add_argument("--brk", type=float, default=0.001)
    p.add_argument("--level-tol", type=float, default=0.0015)
    p.add_argument("--min-touches", type=int, default=2)
    p.add_argument("--cooldown", type=int, default=6)
    p.add_argument("--horizon-ms", type=int, default=900_000)
    p.add_argument("--window-ms", type=int, default=300_000, help="pre-break dynamic window")
    p.add_argument("--tol", type=float, default=0.0015, help="at-level band for book/eaten")
    p.add_argument("--band", type=float, default=0.005, help="through-depth + survivor band")
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--dump", type=Path, required=True)
    args = p.parse_args()

    cfg = {k: getattr(args, k) for k in (
        "bar_ms", "consol_bars", "range_thresh", "brk", "level_tol", "min_touches",
        "cooldown", "horizon_ms", "window_ms", "tol", "band", "good_mfe")}
    jobs = [(args.store, sym, date, cfg)
            for sym in args.symbols for date in sbt.daterange(args.start, args.end)]

    recs = []
    with Pool(args.workers, initializer=_init_worker) as pool:
        for day in pool.imap_unordered(process_day, jobs):
            recs.extend(day)

    def _native(o):
        if isinstance(o, np.generic):
            return o.item()
        raise TypeError(f"not serializable: {type(o)}")

    with open(args.dump, "w") as f:
        for r in recs:
            f.write(json.dumps(r, default=_native) + "\n")
    n = len(recs)
    g = sum(r["good"] for r in recs)
    print("\n" + "=" * 64)
    print(f"  MICRO STUDY  n={n}  good={g} ({g/n:.1%})" if n else "  no breakouts")
    print(f"  dumped {n} -> {args.dump}")


if __name__ == "__main__":
    main()
