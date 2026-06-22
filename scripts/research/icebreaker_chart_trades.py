"""Eyeball check: render gated breakout setups with the SIMULATED TRADE overlaid.

Reads a gated dump (symbol/date/ts_close/side/level), rebuilds bars from the tape,
and for each picked setup draws price candles + level + break + squeeze dots + a
volume-profile side panel, then overlays the actual managed trade: entry marker,
stop line, TP1 line, and the exit point with its reason + realized R. Lets you
confirm by eye that the detector finds breakout-after-squeeze-at-edge setups (and
see why the runners run and the fakeouts fail).

    python scripts/research/icebreaker_chart_trades.py \
        --store /root/trading/data/icebreaker_active \
        --cache '/root/trading/tmp/ib_gated_*.jsonl' \
        --symbols WIFUSDT FARTCOINUSDT --n 6 --out /root/trading/tmp/charts_trades
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import random
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
from src.strategy import robust_levels as rl


def walk_trade(ts, px, ts0, side, level, cfg):
    """Managed scalp walk that RECORDS markers for plotting.

    Returns dict(entry, stop0, tp1_px, exits=[(ts,px,frac,reason)], best, R, gross).
    Mirrors icebreaker_exit_sim.simulate_exit but also keeps exit timestamps."""
    i = bisect_left(ts, ts0)
    if i >= len(ts):
        return None
    entry = float(px[i])
    dirn = 1.0 if side == "long" else -1.0
    stop0 = level * (1 - cfg["buffer"]) if side == "long" else level * (1 + cfg["buffer"])
    risk = dirn * (entry - stop0) / entry
    tp1_px = entry * (1 + dirn * cfg["tp1"])
    gb, f1 = cfg["trail_giveback"], cfg["f1"]
    t_end = ts0 + cfg["horizon_ms"]

    exits, remaining, tp1_done, best = [], 1.0, False, entry
    j, last, last_ts = i, entry, ts0
    while j < len(ts) and ts[j] <= t_end:
        p = float(px[j]); last = p; last_ts = int(ts[j])
        if dirn * (p - best) > 0:
            best = p
        if tp1_done:
            trail = best * (1 - dirn * gb)
            eff = max(trail, entry) if side == "long" else min(trail, entry)
            if dirn * (p - eff) <= 0:
                exits.append((int(ts[j]), eff, remaining, "trail")); remaining = 0.0; break
        else:
            if dirn * (p - stop0) <= 0:
                exits.append((int(ts[j]), stop0, remaining, "stop")); remaining = 0.0; break
            if dirn * (p - tp1_px) >= 0:
                exits.append((int(ts[j]), tp1_px, f1, "tp1")); remaining -= f1
                tp1_done = True; best = p
        j += 1
    if remaining > 1e-9:
        exits.append((last_ts, last, remaining, "horizon"))

    gross = sum(g * dirn * (pp - entry) / entry for _, pp, g, _ in exits)
    fee_units = 1.0 + sum(g for _, _, g, _ in exits)
    net = gross - cfg["fee_side"] * fee_units
    return {"entry": entry, "stop0": stop0, "tp1_px": tp1_px, "exits": exits,
            "best": best, "R": (net / risk) if risk > 1e-9 else float("nan"),
            "gross": gross, "net": net}


def render(bars, bar_ms, rec, tr, out_path, lookback):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    side = rec["side"]; L = rec["level"]
    bts = np.array([b["ts"] for b in bars])
    i = int(np.searchsorted(bts, rec["ts_close"] - bar_ms))   # break bar
    i = min(max(i, 1), len(bars) - 1)
    c = np.array([b["close"] for b in bars]); v = np.array([b["volume"] for b in bars])
    h = np.array([b["high"] for b in bars]); l = np.array([b["low"] for b in bars])
    lo, hi = max(0, i - 300), min(len(bars), i + 120)
    chunk = bars[lo:hi]
    sq = rl.ttm_squeeze(h, l, c)

    fig, (ax, axv) = plt.subplots(1, 2, figsize=(15, 7), facecolor="#15151f",
                                  gridspec_kw={"width_ratios": [4, 1]}, sharey=True)
    ax.set_facecolor("#15151f"); axv.set_facecolor("#15151f")
    for k, b in enumerate(chunk):
        o2, h2, l2, c2 = b["open"], b["high"], b["low"], b["close"]
        col = "#26a69a" if c2 >= o2 else "#ef5350"
        ax.bar(k, abs(c2 - o2), bottom=min(o2, c2), width=0.6, color=col, edgecolor=col)
        ax.plot([k, k], [l2, h2], color=col, linewidth=0.7)
        if sq[lo + k]:
            ax.plot(k, l2 - (h2 - l2) * 0.3, marker="o", color="#ab47bc", markersize=1.5)

    ax.axhline(L, color="#ffd54f", linewidth=1.4, linestyle="--", label="level")
    ax.axhline(tr["stop0"], color="#ef5350", linewidth=1.0, linestyle=":", label="stop")
    ax.axhline(tr["tp1_px"], color="#26a69a", linewidth=1.0, linestyle=":", label="TP1")
    xb = i - lo
    ax.axvline(xb, color="#42a5f5", linewidth=1.0, linestyle=":")
    # entry marker
    em = "^" if side == "long" else "v"
    ax.plot(xb, tr["entry"], marker=em, color="#ffffff", markersize=11,
            markeredgecolor="#42a5f5", zorder=5)
    # exit markers
    for ets, epx, frac, reason in tr["exits"]:
        xe = int(np.searchsorted(bts, ets)) - lo
        xe = min(max(xe, 0), len(chunk) - 1)
        ecol = {"tp1": "#26a69a", "trail": "#ffd54f", "stop": "#ef5350",
                "horizon": "#90a4ae"}[reason]
        ax.plot(xe, epx, marker="X", color=ecol, markersize=11, markeredgecolor="#000",
                zorder=6)
        ax.annotate(f"{reason} {frac:.0%}", (xe, epx), color=ecol, fontsize=7,
                    xytext=(3, 4), textcoords="offset points")

    t = datetime.fromtimestamp(rec["ts_close"] / 1000, timezone.utc).strftime("%m-%d %H:%M")
    verdict = "RUNNER" if rec.get("good") else "fakeout"
    ax.set_title(f"{rec['symbol']} {side.upper()} {t}UTC [{verdict}]  "
                 f"MFE {rec['mfe']:.2%}  net {tr['net']:+.2%}  R {tr['R']:+.2f}",
                 color="#eee", fontsize=11)
    ax.text(0.01, 0.99,
            f"touches {rec.get('touches','?')}  shelf {rec.get('shelf',0):.1%}  "
            f"air {rec.get('air',0):.2f}  edge {rec.get('edge_pos',0):.2f}  "
            f"pocD {rec.get('poc_dist',0)}  one_sided {rec.get('one_sided',0):.2f}  "
            f"sqz {rec.get('squeeze_frac',0):.0%}",
            transform=ax.transAxes, va="top", color="#bbb", fontsize=8, family="monospace")
    ax.legend(loc="lower left", fontsize=7, facecolor="#222", labelcolor="#ccc")
    ax.tick_params(colors="#888")

    centers, mass = rl.volume_profile(c[i - lookback:i], v[i - lookback:i],
                                      lo=l[i - lookback:i].min(), hi=h[i - lookback:i].max(),
                                      bins=120)
    if len(centers):
        axv.barh(centers, mass, height=(centers[1] - centers[0]) if len(centers) > 1 else 1,
                 color="#5c6bc0", alpha=0.7)
        axv.axhline(L, color="#ffd54f", linewidth=1.1, linestyle="--")
    axv.set_title("volume profile", color="#888", fontsize=9)
    axv.tick_params(colors="#888")
    plt.tight_layout()
    fig.savefig(out_path, dpi=92, facecolor="#15151f")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-31")
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--lookback", type=int, default=480)
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--buffer", type=float, default=0.002)
    p.add_argument("--tp1", type=float, default=0.006)
    p.add_argument("--f1", type=float, default=0.5)
    p.add_argument("--trail-giveback", type=float, default=0.004)
    p.add_argument("--horizon-ms", type=int, default=1_800_000)
    p.add_argument("--fee-side", type=float, default=0.0002)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    cfg = {"buffer": args.buffer, "tp1": args.tp1, "f1": args.f1,
           "trail_giveback": args.trail_giveback, "horizon_ms": args.horizon_ms,
           "fee_side": args.fee_side}

    recs = []
    for fp in sorted(glob.glob(args.cache)):
        recs.extend(json.loads(l) for l in open(fp) if l.strip())
    if args.symbols:
        recs = [r for r in recs if r["symbol"] in args.symbols]
    random.seed(args.seed)
    runners = [r for r in recs if r.get("good")]
    fakes = [r for r in recs if not r.get("good")]
    runners.sort(key=lambda r: -r["mfe"])          # best runners first
    random.shuffle(fakes)
    pick = runners[:args.n // 2] + fakes[:args.n - args.n // 2]

    args.out.mkdir(parents=True, exist_ok=True)
    store = Path(args.store)
    by_sym = {}
    for r in pick:
        by_sym.setdefault(r["symbol"], []).append(r)

    for sym, srecs in by_sym.items():
        bars, tape_by_date = [], {}
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            t_ts, t_pr, t_qty, _ = mc.load_trades_arr(store, sym, date)
            if len(t_ts) == 0:
                continue
            bars.extend(mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms))
            tape_by_date[date] = (t_ts, t_pr)
        for r in srecs:
            tp = tape_by_date.get(r["date"])
            if tp is None:
                continue
            tr = walk_trade(tp[0], tp[1], r["ts_close"], r["side"], r["level"], cfg)
            if tr is None:
                continue
            t = datetime.fromtimestamp(r["ts_close"] / 1000, timezone.utc).strftime("%m%d_%H%M")
            tag = "runner" if r.get("good") else "fake"
            out = args.out / f"{sym}_{t}_{r['side']}_{tag}.png"
            render(bars, args.bar_ms, r, tr, out, args.lookback)
            print(f"  {out.name}  net {tr['net']:+.2%} R {tr['R']:+.2f} "
                  f"exits={[e[3] for e in tr['exits']]}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
