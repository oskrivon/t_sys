"""Phase 5 — the canonical scalper playbook: RETEST + order-flow confirmation.

Web research (Bookmap et al.) says scalpers do NOT enter at the level on the
break; they wait for price to pull back and RETEST the broken level, and enter
when supportive order flow resumes (aggression back in the break direction). We
killed each component separately (retest fill = adverse selection Phase 1.8;
touch-flow = AUC 0.50 Phase 2) but never ran this EXACT end-to-end pipeline.

For each detected break (detect_major_breakouts):
  - arm the level, wait up to retest_window for price to pull back into the
    retest zone [level-buffer , level+retest_tol] (long; mirror for short)
  - FAIL if price falls back through level-buffer first (broken break, no entry)
  - MISS if no retest within the window (the runner gapped away)
  - retest_only : enter taker at first tick in the zone
  - retest_flow : enter taker at first tick in the zone with FAVORABLE signed
                  aggressor flow over a rolling window (buyers stepping back in)
  - exit via the managed simulate_exit (tight stop just under the level)

Adverse-selection check: compare the ORIGINAL break MFE (from ts_close) of
ENTERED vs MISSED setups. If MISSED (no-retest) has higher MFE, the retest
selects AGAINST runners — the structural reason the playbook fails for us.

    python scripts/research/icebreaker_retest_flow.py --store ... --symbols ... --start ... --end ...
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from bisect import bisect_left
from collections import Counter, deque
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
bs = _load("icebreaker_breakout_study", "scripts/research/icebreaker_breakout_study.py")
esim = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")


def spacing(touches, span_bars):
    if not touches or touches < 2 or span_bars is None:
        return None
    return span_bars / (touches - 1)


def is_clean(b):
    sp = spacing(b.get("touches"), b.get("span_bars"))
    return b.get("one_sided", 0) >= 0.9 and sp is not None and sp > 60


def find_retest(ts, px, qty, sd, ts0, side, level, cfg, use_flow):
    """Return (entry_idx, reason). reason in {retest, retest_flow, failed, no_retest}."""
    dirn = 1.0 if side == "long" else -1.0
    i0 = bisect_left(ts, ts0)
    t_end = ts0 + cfg["retest_window_ms"]
    rtol, buf, W = cfg["retest_tol"], cfg["buffer"], cfg["flow_win_ms"]
    dq, run = deque(), 0.0
    j = i0
    while j < len(ts) and ts[j] <= t_end:
        p = float(px[j])
        sq = float(qty[j]) * (1.0 if sd[j] == 1 else -1.0)
        dq.append((ts[j], sq)); run += sq
        cutoff = ts[j] - W
        while dq and dq[0][0] < cutoff:
            run -= dq.popleft()[1]
        if side == "long":
            failed = p < level * (1 - buf)
            in_zone = p <= level * (1 + rtol)
        else:
            failed = p > level * (1 + buf)
            in_zone = p >= level * (1 - rtol)
        if failed:
            return (None, "failed")
        if in_zone:
            if not use_flow:
                return (j, "retest")
            if dirn * run > 0:                      # buyers (long) back in control
                return (j, "retest_flow")
        j += 1
    return (None, "no_retest")


def run_mode(brk_list, ts, px, qty, sd, cfg, use_flow):
    """Returns list of dicts per break: {reason, net?, clean, mon, brk_mfe}."""
    out = []
    for bk in brk_list:
        ts0, side, lvl = bk["ts_close"], bk["side"], bk["level"]
        mv = bs.measure_move(ts, px, ts0, side, cfg["horizon_ms"])
        brk_mfe = mv[1] if mv else None
        clean = is_clean(bk)
        mon = datetime.fromtimestamp(ts0 / 1000, timezone.utc).strftime("%Y-%m")
        ei, reason = find_retest(ts, px, qty, sd, ts0, side, lvl, cfg, use_flow)
        rec = {"reason": reason, "clean": clean, "mon": mon, "brk_mfe": brk_mfe, "net": None}
        if ei is not None:
            ts_entry = int(ts[ei])
            r = esim.simulate_exit(ts, px, ts_entry, side, lvl, cfg)
            if r is not None:
                rec["net"] = r["gross"] - cfg["fee_side"] * r["fee_units"]
                rec["exit_reason"] = r["reason"]
        out.append(rec)
    return out


def report(name, recs):
    entered = [r for r in recs if r["net"] is not None]
    reasons = Counter(r["reason"] for r in recs)
    n = len(recs)
    print(f"\n[{name}] breaks={n}  outcomes={dict(reasons)}")
    if entered:
        nets = [r["net"] for r in entered]
        wins = sum(1 for x in nets if x > 0)
        exr = Counter(r.get("exit_reason") for r in entered)
        print(f"   ENTERED={len(entered)} ({len(entered)/n*100:.0f}%)  taker_mean={sum(nets)/len(nets)*100:+.3f}% "
              f"sum={sum(nets)*100:+.1f}%  WR={wins/len(entered)*100:.0f}%  exits={dict(exr)}")
    # adverse selection: original break MFE of entered vs missed(no_retest)
    def gr(sub):
        v = [r["brk_mfe"] for r in sub if r["brk_mfe"] is not None]
        if not v:
            return "n/a"
        good = sum(1 for x in v if x >= 0.01)
        return f"good@1%={good/len(v)*100:.0f}% medMFE={sorted(v)[len(v)//2]*100:.2f}% (n={len(v)})"
    miss = [r for r in recs if r["reason"] == "no_retest"]
    ent = [r for r in recs if r["net"] is not None]
    fail = [r for r in recs if r["reason"] == "failed"]
    print(f"   ADVERSE-SEL  entered: {gr(ent)} | missed(no-retest): {gr(miss)} | failed: {gr(fail)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--fee-side", type=float, default=0.00055)
    p.add_argument("--buffer", type=float, default=0.001)
    p.add_argument("--tp1", type=float, default=0.006)
    p.add_argument("--f1", type=float, default=0.5)
    p.add_argument("--trail-giveback", type=float, default=0.004)
    p.add_argument("--horizon-ms", type=int, default=1_800_000)
    p.add_argument("--retest-window-ms", type=int, default=1_800_000)
    p.add_argument("--retest-tol", type=float, default=0.0015)
    p.add_argument("--flow-win-ms", type=int, default=15_000)
    args = p.parse_args()

    cfg = {"buffer": args.buffer, "tp1": args.tp1, "f1": args.f1,
           "trail_giveback": args.trail_giveback, "horizon_ms": args.horizon_ms,
           "fee_side": args.fee_side, "retest_window_ms": args.retest_window_ms,
           "retest_tol": args.retest_tol, "flow_win_ms": args.flow_win_ms}

    store = Path(args.store)
    all_recs = {"retest_only": {"ALL": [], "CLEAN": []},
                "retest_flow": {"ALL": [], "CLEAN": []}}
    month_recs = {}

    for sym in args.symbols:
        t_ts, t_pr, t_qty, t_sd = [], [], [], []
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            a, b, c, d = mc.load_trades_arr(store, sym, date)
            if len(a):
                t_ts.append(a); t_pr.append(b); t_qty.append(c); t_sd.append(d)
        if not t_ts:
            print(f"  {sym}: no tape", flush=True); continue
        t_ts = np.concatenate(t_ts); t_pr = np.concatenate(t_pr)
        t_qty = np.concatenate(t_qty); t_sd = np.concatenate(t_sd)
        bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)
        bks = major.detect_major_breakouts(
            bars, args.bar_ms, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
            min_span_bars=120, brk=0.0015, cooldown=30, near_bars=30,
            near_tol=0.003, one_sided=0.70)
        for mode, use_flow in (("retest_only", False), ("retest_flow", True)):
            recs = run_mode(bks, t_ts, t_pr, t_qty, t_sd, cfg, use_flow)
            all_recs[mode]["ALL"].extend(recs)
            all_recs[mode]["CLEAN"].extend([r for r in recs if r["clean"]])
            for r in recs:
                month_recs.setdefault((r["mon"], mode), []).append(r)
        print(f"  [{sym}] breaks={len(bks)}", flush=True)

    print("\n" + "=" * 78)
    print("POOLED — RETEST + flow confirmation (taker, managed exit)")
    print("=" * 78)
    for mode in ("retest_only", "retest_flow"):
        for pop in ("ALL", "CLEAN"):
            report(f"{mode}/{pop}", all_recs[mode][pop])

    print("\n" + "=" * 78)
    print("PER MONTH (ALL population)")
    print("=" * 78)
    for mon in sorted(set(k[0] for k in month_recs)):
        for mode in ("retest_only", "retest_flow"):
            report(f"{mon}/{mode}", month_recs.get((mon, mode), []))


if __name__ == "__main__":
    main()
