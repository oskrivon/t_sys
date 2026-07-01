"""DECISIVE tradability gate for the FADE (bounce) extension of icebreaker.

The klines fade sim (icebreaker_fade_sim.py) found: fading the LOW/NEGATIVE-mom-k0 tail the
breakout discards is net-positive at MAKER fees (+0.037%/trade, WR 59%, 21/24 months on 2y),
dead at taker. That number ASSUMES a guaranteed fill at the break close — the same optimistic
assumption that killed the Phase-1.7 maker edge once fill-risk was modeled (icebreaker_maker_fill.py).

This runs the real-tape fill gate for the fade, on the 9-meme x Feb-May tape store:
  * detect breaks on 1m bars built from the tape (identical detector/mom-k0 to mom_gate),
    keep only mom-k0 < --mom-max (the fade tail),
  * fade side = OPPOSITE the break (break up -> fade short; break down -> fade long),
  * post a PASSIVE maker limit on the fade's passive side (short: sell above; long: buy below)
    at entry*(1 -/+ offset), fill ONLY on a genuine tape touch within the window,
  * managed exit with an ENTRY-ANCHORED fixed stop (the level is favourable for a fade, so it
    can't be the stop) — same tp0.6/trail0.4/h30 as everywhere,
  * report fill-rate, maker/taker net per FILLED trade, per-coin & per-month sign, and the
    adverse-selection split: fade-good-rate(mfe>=tp) of FILLED vs MISSED (toxic if miss>fill).

Offset 0 == post at the break close (the klines-sim assumption, now fill-gated).

    python scripts/research/icebreaker_fade_fill.py \
        --store /root/trading/data/icebreaker_active \
        --symbols 1000BONKUSDT 1000FLOKIUSDT 1000PEPEUSDT DOGEUSDT FARTCOINUSDT \
                  ORDIUSDT POPCATUSDT WIFUSDT WLDUSDT \
        --start 2026-02-01 --end 2026-06-01 --mom-max 0.0
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

MOM_WIN = 30
FADE_CFG = {"buffer": 0.008, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000}   # buffer = fixed stop distance BEYOND entry (entry-anchored)
OFFSETS = [0.0, 0.0005, 0.001, 0.002]  # how far into the poke the passive limit rests
WINDOWS = [300_000, 900_000]           # 5m / 15m to get filled


def fade_side(break_side):
    return "short" if break_side == "long" else "long"


def limit_price(trade_side, entry, offset):
    """Passive maker limit: fade-long -> buy below; fade-short -> sell above."""
    dirn = 1.0 if trade_side == "long" else -1.0
    return entry * (1 - dirn * offset)


def find_fill(ts, px, ts0, trade_side, limit, window_ms):
    """First genuine tape touch of a resting limit within the window. Fade-long buy-limit
    fills when price <= limit; fade-short sell-limit fills when price >= limit."""
    i = bisect_left(ts, ts0)
    t_end = ts0 + window_ms
    long = trade_side == "long"
    while i < len(ts) and ts[i] <= t_end:
        p = float(px[i])
        if (long and p <= limit) or (not long and p >= limit):
            return i, int(ts[i])
        i += 1
    return None


def fade_walk_exit(ts, px, entry_i, entry, trade_side, cfg):
    """Managed exit from an explicit entry, ENTRY-ANCHORED fixed stop (fade geometry)."""
    long = trade_side == "long"
    dirn = 1.0 if long else -1.0
    stop0 = entry * (1 - dirn * cfg["buffer"])
    tp1_px = entry * (1 + dirn * cfg["tp1"])
    gb, f1 = cfg["trail_giveback"], cfg["f1"]
    t_end = ts[entry_i] + cfg["horizon_ms"]
    legs, remaining, tp1_done, best = [], 1.0, False, entry
    reason, j, last = "horizon", entry_i, entry
    while j < len(ts) and ts[j] <= t_end:
        p = float(px[j]); last = p
        if dirn * (p - best) > 0:
            best = p
        if tp1_done:
            trail = best * (1 - dirn * gb)
            eff = max(trail, entry) if long else min(trail, entry)
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
    return {"gross": gross, "exit_units": exit_units, "reason": reason,
            "mfe": dirn * (best - entry) / entry}


def net_pnl(res, fee_entry, fee_exit):
    return res["gross"] - fee_entry - fee_exit * res["exit_units"]


def good(mfes, gm):
    return sum(1 for m in mfes if m >= gm) / len(mfes) if mfes else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--mom-max", type=float, default=0.0, help="fade only setups with mom-k0 < this")
    p.add_argument("--fee-maker", type=float, default=0.0002)
    p.add_argument("--fee-taker", type=float, default=0.00055)
    args = p.parse_args()
    store = Path(args.store)
    fm, ft = args.fee_maker, args.fee_taker
    gm = FADE_CFG["tp1"]   # fade "good" = reached TP-distance favorable excursion

    tagged = []
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
        t_ts = np.concatenate(t_ts); t_pr = np.concatenate(t_pr); t_qty = np.concatenate(t_qty)
        bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)
        bar_ts = np.array([b["ts"] for b in bars])
        bar_cl = np.array([b["close"] for b in bars])
        bks = major.detect_major_breakouts(
            bars, args.bar_ms, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
            min_span_bars=120, brk=0.0015, cooldown=30, near_bars=30,
            near_tol=0.003, one_sided=0.70)
        kept = 0
        for bk in bks:
            ts0 = bk["ts_close"]; bside = bk["side"]
            bidx = int(np.searchsorted(bar_ts, ts0))
            if bidx >= len(bar_ts) or bar_ts[bidx] != ts0 or bidx < MOM_WIN:
                continue
            sign = 1.0 if bside == "long" else -1.0
            mom = sign * (bar_cl[bidx] / bar_cl[bidx - MOM_WIN] - 1.0)
            if mom >= args.mom_max:
                continue
            fs = fade_side(bside)
            ei = bisect_left(t_ts, ts0)
            if ei >= len(t_ts):
                continue
            entry_px = float(t_pr[ei])
            base = fade_walk_exit(t_ts, t_pr, ei, entry_px, fs, FADE_CFG)
            rec = {"sym": sym, "month": datetime.fromtimestamp(ts0 / 1000, timezone.utc).strftime("%Y-%m"),
                   "mom": mom, "fs": fs, "taker": base, "fills": {}}
            for off in OFFSETS:
                lim = limit_price(fs, entry_px, off)
                for win in WINDOWS:
                    f = find_fill(t_ts, t_pr, ts0, fs, lim, win)
                    rec["fills"][(off, win)] = (
                        fade_walk_exit(t_ts, t_pr, f[0], lim, fs, FADE_CFG) if f else None)
            tagged.append(rec); kept += 1
        print(f"  [{sym}] breaks={len(bks)} fade(mom<{args.mom_max})={kept}", flush=True)

    n = len(tagged)
    print("\n" + "=" * 104)
    print(f"  FADE FILL-RISK  n_setups={n} (mom<{args.mom_max})  "
          f"exit=stop{FADE_CFG['buffer']*100:.1f}/tp{FADE_CFG['tp1']*100:.1f}/"
          f"tr{FADE_CFG['trail_giveback']*100:.1f}/h{FADE_CFG['horizon_ms']//60000}  "
          f"maker={fm*100:.3f}% taker={ft*100:.3f}%")
    print("=" * 104)
    if n == 0:
        return

    def summ(nets):
        return (sum(nets) / len(nets) * 100, sorted(nets)[len(nets) // 2] * 100,
                sum(x > 0 for x in nets) / len(nets) * 100, sum(nets) * 100)

    for tag, fe, fx in (("TAKER guaranteed@close", ft, ft), ("MAKER guaranteed@close", fm, fm)):
        nets = [net_pnl(t["taker"], fe, fx) for t in tagged]
        mean, med, wr, s = summ(nets)
        print(f"  {tag:24s} fill=100.0%  mean={mean:+.3f}%  med={med:+.3f}%  WR={wr:3.0f}%  sum={s:+.1f}%")

    print(f"\n  {'offset':>7s} {'win':>4s} {'fill%':>6s} {'n':>5s} {'mk/tk%':>8s} {'mk/mk%':>8s} "
          f"{'med%':>7s} {'WR%':>5s} {'mon+':>6s} {'coin+':>6s} {'gr_fill':>7s} {'gr_miss':>7s}")
    for off in OFFSETS:
        for win in WINDOWS:
            filled = [(t, t["fills"][(off, win)]) for t in tagged if t["fills"][(off, win)]]
            missed = [t for t in tagged if not t["fills"][(off, win)]]
            nf = len(filled)
            if nf == 0:
                print(f"  {off*100:6.2f}% {win//1000:3d}s   0.0%     0"); continue
            mt = [net_pnl(ex, fm, ft) for _, ex in filled]
            mm = [net_pnl(ex, fm, fm) for _, ex in filled]
            bym = defaultdict(list); byc = defaultdict(float)
            for (t, _), x in zip(filled, mt):
                bym[t["month"]].append(x); byc[t["sym"]] += x
            monp = sum(1 for v in bym.values() if sum(v) / len(v) > 0)
            coinp = sum(1 for v in byc.values() if v > 0)
            grf = good([ex["mfe"] for _, ex in filled], gm)
            grm = good([t["taker"]["mfe"] for t in missed], gm) if missed else float("nan")
            _, med, wr, _ = summ(mt)
            print(f"  {off*100:6.2f}% {win//1000:3d}s {nf/n:6.1%} {nf:5d} "
                  f"{sum(mt)/nf*100:+8.3f} {sum(mm)/nf*100:+8.3f} {med:+7.3f} {wr:5.0f} "
                  f"{monp:2d}/{len(bym):<3d} {coinp:2d}/{len(byc):<3d} {grf:7.1%} {grm:7.1%}")

    print("\n  mk/tk = maker entry + taker exit; mk/mk = maker both.")
    print("  gr_fill/gr_miss = fade reached TP-distance excursion among FILLED vs MISSED")
    print("  (adverse selection if gr_miss > gr_fill: the reverters are the ones you DON'T fill).")


if __name__ == "__main__":
    main()
