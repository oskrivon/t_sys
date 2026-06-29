"""Final-round: HONEST sub-minute entry on the alive cell (CLEAN x forceful break).

mom_gate showed CLEAN x mom-HIGH is gross-positive 3/3 months but net sits at the cost
boundary, bottlenecked by the SPIKE-TOP entry (market @ 1m break-close, 54% stops). The
customer's hypothesis: enter in the first seconds of the break (better price than the 1m
close) once intra-bar momentum confirms.

To be HONEST (not re-derive the +0.28% lookahead ceiling of Phase 2a confirmed-cross), we
ARM the clean level BEFORE any break (pre-break geometry only) and enter at the FIRST tape
cross + delta-t -- fakeout pokes INCLUDED. A sub-minute velocity gate (signed price move
over [cross, cross+dt]) is the intra-bar analog of mom-HIGH. We sweep dt and compare to the
spike-top (bar-close) baseline, split by velocity tercile, clean vs all, per month.

Wall prior (confirm_sweep, Phase 2d): any wait -> give-back -> entry drifts off the level ->
level-anchored stop inflates avgLoss. Sub-minute is finer points on that curve. This run
gives the explicit honest curve incl a true OOS month (Feb).

    python scripts/research/icebreaker_subminute.py \
        --store /root/trading/data/icebreaker_active --symbols ... \
        --start 2026-03-01 --end 2026-03-31 --tag MARCH
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from bisect import bisect_left
from collections import defaultdict
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
major = _load("icebreaker_major", "scripts/research/icebreaker_major.py")
esim = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")

EXIT_CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000, "fee_side": 0.00055}
DTS_MS = [0, 2000, 5000, 10000, 20000]
MAX_CROSS_WIN_MS = 4 * 3600 * 1000          # level must cross within 4h of arming


def detect_armed_clean(bars, bar_ms, *, lookback, swing_w, tol, min_touches,
                       min_span_bars, near_bars, near_tol, one_sided_min):
    """Arm each distinct level ONCE, when it first qualifies (multi-touch cluster, price
    hugging, not yet crossed). Pre-break geometry only -> no lookahead. clean flag =
    spacing-clean (one_sided>=0.9 & spacing>60). Fakeouts are included downstream by
    entering at the first cross after arm_ts."""
    sh, sl = major.find_swings(bars, swing_w)
    armed, seen = [], []
    for i in range(lookback, len(bars)):
        c, lo = bars[i]["close"], i - lookback
        closes = [bars[j]["close"] for j in range(lo, i)]
        for side, swings, pf in (("long", sh, "high"), ("short", sl, "low")):
            pts = [(bars[j][pf], j) for j in swings if lo <= j < i]
            if len(pts) < min_touches:
                continue
            for lvl, mem in major.cluster_levels(pts, tol):
                if len(mem) < min_touches:
                    continue
                span = max(m[1] for m in mem) - min(m[1] for m in mem)
                if span < min_span_bars:
                    continue
                origin = sum(1 for x in closes if (x < lvl if side == "long" else x > lvl))
                os_ = origin / len(closes)
                if os_ < one_sided_min:
                    continue
                recent = bars[max(0, i - near_bars):i]
                if min(abs(b["close"] - lvl) / lvl for b in recent) > near_tol:
                    continue
                on_origin = (c < lvl) if side == "long" else (c > lvl)
                if not on_origin:
                    continue
                if any(s == side and abs(p - lvl) / lvl <= tol for s, p in seen):
                    continue
                seen.append((side, lvl))
                touches = len(mem)
                sp = span / (touches - 1) if touches > 1 else 0.0
                armed.append({"level": lvl, "side": side,
                              "arm_ts": bars[i]["ts"] + bar_ms, "touches": touches,
                              "span_bars": int(span), "one_sided": round(os_, 2),
                              "spacing": sp, "clean": os_ >= 0.9 and sp > 60})
    return armed


def first_cross(t_ts, t_pr, start_ts, side, level, window_ms):
    i = bisect_left(t_ts, start_ts)
    end = start_ts + window_ms
    long = side == "long"
    while i < len(t_ts) and t_ts[i] <= end:
        p = float(t_pr[i])
        if (long and p >= level) or (not long and p <= level):
            return i
        i += 1
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--fee-side", type=float, default=0.00055)
    p.add_argument("--tag", default="")
    args = p.parse_args()
    store = Path(args.store)

    rows = []           # one per armed level that crossed
    for sym in args.symbols:
        t_ts, t_pr, t_qty = [], [], []
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            a, b, c, _ = mc.load_trades_arr(store, sym, date)
            if len(a):
                t_ts.append(a); t_pr.append(b); t_qty.append(c)
        if not t_ts:
            print(f"  {sym}: no tape", flush=True); continue
        t_ts = np.concatenate(t_ts); t_pr = np.concatenate(t_pr)
        bars = mc.bars_np(t_ts, t_pr, np.concatenate(t_qty), args.bar_ms)
        armed = detect_armed_clean(
            bars, args.bar_ms, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
            min_span_bars=120, near_bars=30, near_tol=0.003, one_sided_min=0.70)
        nco = 0
        for a in armed:
            ci0 = first_cross(t_ts, t_pr, a["arm_ts"], a["side"], a["level"],
                              MAX_CROSS_WIN_MS)
            if ci0 is None:
                continue
            t_cross = int(t_ts[ci0])
            dirn = 1.0 if a["side"] == "long" else -1.0
            row = {"sym": sym, "clean": a["clean"],
                   "month": datetime.fromtimestamp(t_cross / 1000, timezone.utc).strftime("%Y-%m")}
            # entry-timing curve: entry at cross + dt
            for dt in DTS_MS:
                ts_entry = t_cross + dt
                # sub-minute velocity over [cross, cross+dt]
                je = bisect_left(t_ts, ts_entry)
                if je >= len(t_pr):
                    je = len(t_pr) - 1
                vel = dirn * (float(t_pr[je]) - float(t_pr[ci0])) / float(t_pr[ci0]) if dt else 0.0
                r = esim.simulate_exit(t_ts, t_pr, ts_entry, a["side"], a["level"], EXIT_CFG)
                if r is None:
                    row[f"dt{dt}"] = None
                    continue
                net = r["gross"] - args.fee_side * r["fee_units"]
                row[f"dt{dt}"] = {"net": net, "gross": r["gross"], "mfe": r["mfe"], "vel": vel}
            rows.append(row); nco += 1
        print(f"  [{sym}] armed={len(armed)} crossed={nco}", flush=True)

    # ---- report: per dt, clean vs all, split by velocity tercile (vel from dt=5s) ----
    print("\n" + "#" * 92)
    print(f"  SUB-MINUTE ENTRY  {args.tag}  armed-clean cross (fakeouts INCLUDED)  "
          f"n={len(rows)}")
    print("  entry = first cross + dt;  stop@level-buffer;  exit buf0.1/tp0.6/tr0.4/h30;  taker")
    print("#" * 92)

    def stat(rs, dt):
        vals = [r[f"dt{dt}"] for r in rs if r.get(f"dt{dt}")]
        if not vals:
            return None
        nets = [v["net"] for v in vals]
        n = len(nets)
        wr = sum(1 for x in nets if x > 0) / n
        return (n, sum(nets) / n, sum(v["gross"] for v in vals) / n, wr,
                sorted(v["mfe"] for v in vals)[n // 2])

    for pop in ("ALL", "CLEAN"):
        rs = rows if pop == "ALL" else [r for r in rows if r["clean"]]
        print(f"\n-- {pop} (n={len(rs)}) --  dt:   net%    gross%   WR   medMFE%")
        for dt in DTS_MS:
            s = stat(rs, dt)
            if s:
                print(f"   dt={dt//1000:>2}s  n={s[0]:<4} net={s[1]*100:+.3f}  "
                      f"gross={s[2]*100:+.3f}  WR={s[3]*100:.0f}%  medMFE={s[4]*100:.2f}")

    # velocity gate: tercile by vel measured at dt=5s, then realized net at dt=5s
    cl = [r for r in rows if r["clean"] and r.get("dt5000")]
    cl = [r for r in cl if r["dt5000"]["vel"] == r["dt5000"]["vel"]]
    if len(cl) >= 6:
        cl.sort(key=lambda r: r["dt5000"]["vel"])
        t = len(cl) // 3
        groups = {"v-LOW": cl[:t], "v-MID": cl[t:2 * t], "v-HIGH": cl[2 * t:]}
        print(f"\n-- CLEAN x sub-minute-velocity tercile (vel@5s), entry@5s, taker --")
        for g, rs in groups.items():
            s = stat(rs, 5000)
            if s:
                coins = defaultdict(float)
                for r in rs:
                    coins[r["sym"]] += r["dt5000"]["net"]
                cplus = sum(1 for v in coins.values() if v > 0)
                print(f"   {g:<7} n={s[0]:<4} net={s[1]*100:+.3f}  gross={s[2]*100:+.3f}  "
                      f"WR={s[3]*100:.0f}%  medMFE={s[4]*100:.2f}  coins+={cplus}/{len(coins)}")


if __name__ == "__main__":
    main()
