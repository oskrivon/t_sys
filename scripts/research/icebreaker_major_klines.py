"""Phase-3 Task 2: does spacing-clean selection reproduce on MAJORS (non-meme)?

Reuses the meme detector (detect_major_breakouts) on 1m OHLC kline bars, computes
the 'good' label from kline-path MFE (high/low over a 15m horizon), then asks:
  - raw good@1% : base vs spacing-clean (one_sided>=0.9 & spacing>60) lift per month
  - vol-norm    : good_vn = MFE/daily_vol >= k (global k calibrated to ~11% pooled),
                  base vs clean lift  <-- the apples-to-apples vs the meme +4pp finding

Majors are low-vol so raw good@1% is sparse/noisy; the vol-norm lift is the real test.
Input: data/klines_major_{mar,apr,may}/<SYM>.jsonl (1m OHLC, from icebreaker_fetch_ohlc.py).
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


major = _load("icebreaker_major", "scripts/research/icebreaker_major.py")

MONTHS = [("MAR", "data/klines_major_mar"), ("APR", "data/klines_major_apr"), ("MAY", "data/klines_major_may")]
BAR_MS = 60_000
HORIZON_MS = 900_000  # 15m, matches meme run
GOOD_MFE = 0.01       # 1%, matches meme good field


def load_bars(d, sym):
    p = Path(d) / f"{sym}.jsonl"
    if not p.exists():
        return []
    bars = [json.loads(l) for l in open(p) if l.strip()]
    bars.sort(key=lambda b: b["ts"])
    return bars


def kline_mfe(bars, ts_index, ts_close, side):
    """entry = open of the bar at ts_close; MFE/MAE from bar highs/lows over horizon."""
    i = ts_index.get(ts_close)
    if i is None:
        return None
    entry = bars[i]["open"]
    if entry <= 0:
        return None
    hi = lo = entry
    t_end = ts_close + HORIZON_MS
    j = i
    while j < len(bars) and bars[j]["ts"] <= t_end:
        hi = max(hi, bars[j]["high"])
        lo = min(lo, bars[j]["low"])
        j += 1
    if side == "long":
        return entry, (hi - entry) / entry, (entry - lo) / entry
    return entry, (entry - lo) / entry, (hi - entry) / entry


def rvol(bars):
    cl = [b["close"] for b in bars if b["close"] > 0]
    if len(cl) < 10:
        return None
    r = [math.log(cl[k] / cl[k - 1]) for k in range(1, len(cl)) if cl[k - 1] > 0]
    if not r:
        return None
    m = sum(r) / len(r)
    v = sum((x - m) ** 2 for x in r) / len(r)
    return math.sqrt(v) * math.sqrt(1440) * 100


def spacing(r):
    t, sb = r.get("touches"), r.get("span_bars")
    if not t or t < 2 or sb is None:
        return None
    return sb / (t - 1)


def is_clean(r):
    sp = spacing(r)
    return r.get("one_sided", 0) >= 0.9 and sp is not None and sp > 60


SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT"]


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    idx = q * (len(xs) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (idx - lo)


def main():
    all_recs = []  # each: month, symbol, one_sided, touches, span_bars, mfe, good, vol, vn
    per_coin_vol = {}
    for tag, d in MONTHS:
        for sym in SYMS:
            bars = load_bars(d, sym)
            if len(bars) < 600:
                print(f"  [{tag} {sym}] only {len(bars)} bars, skip", flush=True)
                continue
            v = rvol(bars)
            per_coin_vol[(tag, sym)] = v
            ts_index = {b["ts"]: k for k, b in enumerate(bars)}
            bks = major.detect_major_breakouts(
                bars, BAR_MS, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
                min_span_bars=120, brk=0.0015, cooldown=30, near_bars=30,
                near_tol=0.003, one_sided=0.70)
            for b in bks:
                mv = kline_mfe(bars, ts_index, b["ts_close"], b["side"])
                if mv is None:
                    continue
                entry, mfe, mae = mv
                all_recs.append({
                    "month": tag, "symbol": sym, "one_sided": b["one_sided"],
                    "touches": b["touches"], "span_bars": b["span_bars"],
                    "mfe": mfe, "good": int(mfe >= GOOD_MFE),
                    "vol": v, "vn": (mfe / v if v else None)})

    # global k for vol-norm, calibrated to pooled raw-good fraction
    pooled = [r for r in all_recs if r["vn"] is not None]
    frac = sum(r["good"] for r in pooled) / len(pooled) if pooled else 0
    k = quantile([r["vn"] for r in pooled], 1 - frac) if pooled else float("nan")

    def good_vn(r):
        return r["vn"] is not None and r["vn"] >= k

    def rates(rows):
        if not rows:
            return (0, 0, 0.0, 0.0)
        n = len(rows)
        graw = sum(r["good"] for r in rows) / n * 100
        gvn = sum(1 for r in rows if good_vn(r)) / n * 100
        return (n, sum(r["good"] for r in rows), graw, gvn)

    print("=" * 74)
    print(f"MAJORS spacing test — {len(all_recs)} setups, {len(SYMS)} coins, 3 months")
    print(f"vol-norm k (MFE/vol) = {k:.4f}, pooled raw-good frac = {frac*100:.1f}%")
    print("=" * 74)

    # per-month base vs clean, raw and vol-norm
    print("\nPER MONTH — base vs spacing-clean (raw good@1% | vol-norm good_vn)")
    print(f"{'mo':<5} {'n':>5} {'base_raw':>9} {'clean_raw':>10} {'liftR':>7} "
          f"{'base_vn':>9} {'clean_vn':>10} {'liftV':>7}")
    print("-" * 74)
    for tag, _ in MONTHS:
        mon = [r for r in all_recs if r["month"] == tag]
        clean = [r for r in mon if is_clean(r)]
        n, _, br, bv = rates(mon)
        cn, _, cr, cv = rates(clean)
        print(f"{tag:<5} {n:>5} {br:>8.1f}% {cr:>9.1f}% {cr-br:>+6.1f} "
              f"{bv:>8.1f}% {cv:>9.1f}% ({cn:>3}) {cv-bv:>+6.1f}")

    # pooled
    clean_all = [r for r in all_recs if is_clean(r)]
    n, _, br, bv = rates(all_recs)
    cn, _, cr, cv = rates(clean_all)
    print("-" * 74)
    print(f"{'ALL':<5} {n:>5} {br:>8.1f}% {cr:>9.1f}% {cr-br:>+6.1f} "
          f"{bv:>8.1f}% {cv:>9.1f}% ({cn:>3}) {cv-bv:>+6.1f}")

    # per-coin setup counts + vol (sanity / regime)
    print("\nPER COIN — setup count (raw good-rate) by month")
    print(f"{'coin':<10} {'MAR':>16} {'APR':>16} {'MAY':>16}")
    print("-" * 62)
    for sym in SYMS:
        cells = []
        for tag, _ in MONTHS:
            rows = [r for r in all_recs if r["month"] == tag and r["symbol"] == sym]
            if rows:
                gr = sum(r["good"] for r in rows) / len(rows) * 100
                v = per_coin_vol.get((tag, sym))
                cells.append(f"{len(rows):>3} ({gr:4.1f}% v{v:4.1f})" if v else f"{len(rows):>3} ({gr:4.1f}%)")
            else:
                cells.append("  --")
        print(f"{sym:<10} {cells[0]:>16} {cells[1]:>16} {cells[2]:>16}")


if __name__ == "__main__":
    main()
