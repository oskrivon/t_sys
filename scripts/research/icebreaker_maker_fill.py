"""Maker fill-risk / adverse-selection test on liquidity-vacuum breakouts.

THE decisive tradability gate for the Phase-1.7 edge. Prior sweeps booked the maker
PnL (+0.08-0.11%/trade) by APPLYING the maker fee while still assuming a GUARANTEED
fill at the breakout-close price (icebreaker_exit_sim.simulate_exit: entry = first
trade at/after ts0). A passive limit does not work that way:

  * it only fills if price actually trades back through it (you post BELOW for a long
    buy, ABOVE for a short sell), and
  * adverse selection: the breakouts that run straight away (the winners of a vacuum
    break) never come back to fill you; the ones that retrace to you are
    disproportionately the FAILED breaks. So conditioning realized PnL on "actually
    got filled" is where the edge can die.

This walks the real trade tape, posts a maker limit at entry*(1 -/+ offset) at the
breakout close, fills ONLY on a genuine touch within an entry window, then runs the
SAME managed exit from the actual fill (price+time). It reports, per (offset, window):
  - fill-rate (how often the passive order is hit at all),
  - realized net per FILLED trade at maker-entry/taker-exit AND maker-both,
  - per-coin sign spread (robustness, not one-coin),
  - the adverse-selection split: good-rate(mfe>=1%) and mean fwd-MFE of FILLED vs
    MISSED setups (does the fill model systematically drop the winners?),
  - the taker-guaranteed-fill baseline (the prior, optimistic number) for contrast.

Offset 0 == post at the breakout close (the prior optimistic assumption, but now
fill-gated); larger offsets == post into the retest; offset 'level' posts the limit
exactly at the broken level (full retest).

    python scripts/research/icebreaker_maker_fill.py \
        --store /root/trading/data/icebreaker_active \
        --cache /root/trading/tmp/ib_liq_empty.jsonl --tag MARCH
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from bisect import bisect_left
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


# ---------------------------------------------------------------------------
# Pure core: maker fill model + managed exit from an explicit entry
# ---------------------------------------------------------------------------
def find_fill(ts, px, ts0, side, limit, entry_window_ms):
    """Passive limit posted at `limit` at ts0; fill on the first trade that touches
    it within the window. Long => buy limit below, fills when price <= limit; short
    => sell limit above, fills when price >= limit. Returns (fill_i, fill_ts) or None.

    The limit must rest on the passive side at post time to be a genuine maker order;
    callers post below (long) / above (short) the close, so the first touch is a real
    crossing of resting liquidity, not an immediate marketable cross."""
    i = bisect_left(ts, ts0)
    t_end = ts0 + entry_window_ms
    long = side == "long"
    while i < len(ts) and ts[i] <= t_end:
        p = float(px[i])
        if (long and p <= limit) or (not long and p >= limit):
            return i, int(ts[i])
        i += 1
    return None


def walk_exit(ts, px, entry_i, entry, side, level, cfg):
    """Managed exit (tiny stop -> TP1 partial -> trail) from an explicit entry index
    and price. Same geometry as icebreaker_exit_sim.simulate_exit but the entry is
    given (so a maker fill price/time can be injected). Returns dict with gross,
    exit_units (sum of exit-leg fractions), reason, mfe; fees applied by caller."""
    dirn = 1.0 if side == "long" else -1.0
    stop0 = level * (1 - cfg["buffer"]) if side == "long" else level * (1 + cfg["buffer"])
    risk = dirn * (entry - stop0) / entry
    tp1_px = entry * (1 + dirn * cfg["tp1"])
    gb, f1 = cfg["trail_giveback"], cfg["f1"]
    t_end = ts[entry_i] + cfg["horizon_ms"]

    legs = []
    remaining, tp1_done, best = 1.0, False, entry
    reason, j, last = "horizon", entry_i, entry
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
    exit_units = sum(g for g, _ in legs)
    return {"gross": gross, "exit_units": exit_units, "risk": risk,
            "reason": reason, "mfe": dirn * (best - entry) / entry}


def net_pnl(res, fee_entry, fee_exit):
    """gross minus entry fee (1 unit) and exit fees (exit_units)."""
    return res["gross"] - fee_entry - fee_exit * res["exit_units"]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
EXIT_CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000}                       # validated 1.7 exit

OFFSETS = [0.0, 0.0005, 0.001, 0.002, "level"]             # limit distance behind close
WINDOWS = [300_000, 900_000]                               # 5m / 15m to get filled


def limit_price(side, entry, level, offset):
    dirn = 1.0 if side == "long" else -1.0
    if offset == "level":
        return level
    return entry * (1 - dirn * offset)


def good(rows, gm=0.01):
    return sum(1 for r in rows if r["mfe"] >= gm) / len(rows) if rows else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True, help="empty-ahead dump (ib_liq_empty*.jsonl)")
    p.add_argument("--fee-maker", type=float, default=0.0002)
    p.add_argument("--fee-taker", type=float, default=0.00055)
    p.add_argument("--tag", default="")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    recs = [json.loads(l) for l in open(args.cache) if l.strip()]
    by_day = defaultdict(list)
    for r in recs:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    # per setup: keep the original record + a per-(offset,window) fill result
    tagged = []
    for (sym, date), setups in sorted(by_day.items()):
        t_ts, t_pr, _, _ = mc.load_trades_arr(store, sym, date)
        if len(t_ts) == 0:
            continue
        for r in setups:
            entry_taker_i = bisect_left(t_ts, r["ts_close"])
            if entry_taker_i >= len(t_ts):
                continue
            entry_taker_px = float(t_pr[entry_taker_i])
            base = walk_exit(t_ts, t_pr, entry_taker_i, entry_taker_px,
                             r["side"], r["level"], EXIT_CFG)
            rec = {"symbol": sym, "side": r["side"], "level": r["level"],
                   "ts_close": r["ts_close"], "mfe_setup": r["mfe"],
                   "taker": base, "fills": {}}
            for off in OFFSETS:
                lim = limit_price(r["side"], entry_taker_px, r["level"], off)
                for win in WINDOWS:
                    f = find_fill(t_ts, t_pr, r["ts_close"], r["side"], lim, win)
                    if f is None:
                        rec["fills"][(off, win)] = None
                    else:
                        fi, fts = f
                        ex = walk_exit(t_ts, t_pr, fi, lim, r["side"], r["level"], EXIT_CFG)
                        rec["fills"][(off, win)] = ex
            tagged.append(rec)

    lines = []

    def emit(s=""):
        lines.append(s); print(s, flush=True)

    n = len(tagged)
    fm, ft = args.fee_maker, args.fee_taker
    emit("=" * 100)
    emit(f"  MAKER FILL-RISK  {args.tag}  n_setups={n}  coins={len(by_day)} day-coins  "
         f"exit=buf{EXIT_CFG['buffer']*100:.1f}/tp{EXIT_CFG['tp1']*100:.1f}/"
         f"tr{EXIT_CFG['trail_giveback']*100:.1f}/h{EXIT_CFG['horizon_ms']//60000}")
    emit("=" * 100)

    # ---- baseline: taker, guaranteed fill at the breakout close (the prior number)
    tk = [t["taker"] for t in tagged]
    for tag, fe, fx in (("TAKER guaranteed", ft, ft), ("MAKER guaranteed (prior)", fm, fm)):
        nets = [net_pnl(b, fe, fx) for b in tk]
        per = defaultdict(float)
        for t, x in zip(tagged, nets):
            per[t["symbol"]] += x
        emit(f"  {tag:26s} n={len(nets):4d}  fill=100.0%  "
             f"mean={sum(nets)/len(nets)*100:+.3f}%  "
             f"med={sorted(nets)[len(nets)//2]*100:+.3f}%  "
             f"win={sum(1 for x in nets if x>0)/len(nets):5.1%}  "
             f"sum={sum(nets)*100:+.1f}%  coins+={sum(1 for v in per.values() if v>0)}/{len(per)}")

    emit("")
    emit(f"  {'offset':>8s} {'win_ms':>7s} {'fill%':>6s} {'n_fill':>6s} "
         f"{'mk/tk mean%':>11s} {'mk/mk mean%':>11s} {'med%':>7s} {'win%':>6s} "
         f"{'sum%':>7s} {'coins+':>7s} {'gr_fill':>7s} {'gr_miss':>7s}")
    for off in OFFSETS:
        for win in WINDOWS:
            filled = [(t, t["fills"][(off, win)]) for t in tagged
                      if t["fills"][(off, win)] is not None]
            missed = [t for t in tagged if t["fills"][(off, win)] is None]
            nf = len(filled)
            if nf == 0:
                emit(f"  {str(off):>8s} {win//1000:>6d}s   0.0%      0")
                continue
            nets_mt = [net_pnl(ex, fm, ft) for _, ex in filled]   # maker entry / taker exit
            nets_mm = [net_pnl(ex, fm, fm) for _, ex in filled]   # maker both
            per = defaultdict(float)
            for (t, _), x in zip(filled, nets_mt):
                per[t["symbol"]] += x
            # adverse selection: forward mfe of the exit walk, filled vs missed
            gr_fill = good([{"mfe": ex["mfe"]} for _, ex in filled])
            gr_miss = good([{"mfe": t["taker"]["mfe"]} for t in missed]) if missed else float("nan")
            offlabel = off if isinstance(off, str) else f"{off*100:.2f}%"
            emit(f"  {offlabel:>8s} {win//1000:>6d}s {nf/n:6.1%} {nf:6d} "
                 f"{sum(nets_mt)/nf*100:+11.3f} {sum(nets_mm)/nf*100:+11.3f} "
                 f"{sorted(nets_mt)[nf//2]*100:+7.3f} "
                 f"{sum(1 for x in nets_mt if x>0)/nf:6.1%} "
                 f"{sum(nets_mt)*100:+7.1f} "
                 f"{sum(1 for v in per.values() if v>0):3d}/{len(per):<3d} "
                 f"{gr_fill:7.1%} {gr_miss:7.1%}")

    emit("")
    emit("  mk/tk = maker entry + taker exit; mk/mk = maker both. gr_fill/gr_miss =")
    emit("  good-rate(setup mfe>=1%) among FILLED vs MISSED — adverse selection if gr_miss>gr_fill.")

    if args.out:
        Path(args.out).write_text("\n".join(lines))
        emit(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
