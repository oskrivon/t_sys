"""Robust-detector breakout study: detect_setups (volume-profile + squeeze +
quality score) -> MFE -> dump (+ optional charts with a volume-profile side panel).

    # detect + dump (tape only, 1m)
    python scripts/research/icebreaker_robust.py \
        --store data/icebreaker_active --symbols DOGEUSDT WIFUSDT ... \
        --start 2026-03-01 --end 2026-03-31 --dump data/ib_robust.jsonl

    # also render charts to eyeball level quality
    ... --charts data/charts/robust --n-charts 16
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
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
from src.strategy import robust_levels as rl


def render(bars, setup, mfe, mae, good, out_path, lookback):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    i = setup.idx
    is_long = setup.side == "long"
    L = setup.level
    lo, hi = max(0, i - 360), min(len(bars), i + 90)
    chunk = bars[lo:hi]
    c = np.array([b["close"] for b in bars])
    v = np.array([b.get("volume", 0.0) for b in bars])
    h = np.array([b["high"] for b in bars]); l = np.array([b["low"] for b in bars])

    fig, (ax, axv) = plt.subplots(1, 2, figsize=(14, 7), facecolor="#15151f",
                                  gridspec_kw={"width_ratios": [4, 1]}, sharey=True)
    ax.set_facecolor("#15151f"); axv.set_facecolor("#15151f")
    sq = rl.ttm_squeeze(h, l, c)
    for k, bar in enumerate(chunk):
        o2, h2, l2, c2 = bar["open"], bar["high"], bar["low"], bar["close"]
        col = "#26a69a" if c2 >= o2 else "#ef5350"
        ax.bar(k, abs(c2 - o2), bottom=min(o2, c2), width=0.6, color=col, edgecolor=col)
        ax.plot([k, k], [l2, h2], color=col, linewidth=0.7)
        if sq[lo + k]:
            ax.plot(k, l2 - (h2 - l2) * 0.3, marker="o", color="#ab47bc", markersize=1.6)
    ax.axhline(L, color="#ffd54f", linewidth=1.5, linestyle="--")
    ax.axvline(i - lo, color="#42a5f5", linewidth=1.3, linestyle=":")
    d = setup.detail
    verdict = "RUNNER" if good else "fakeout"
    t = datetime.fromtimestamp(bars[i]["ts"] / 1000, timezone.utc).strftime("%m-%d %H:%M")
    ax.set_title(f"{setup.side.upper()} {t}UTC [{verdict}]  MFE {mfe:.2%}/MAE {mae:.2%}  "
                 f"score {setup.score:.1f}", color="#eee", fontsize=11)
    ax.text(0.01, 0.99,
            f"touches {d['touches']}  shelf {d.get('shelf',0):.1%}  air {d.get('air',0):.2f}  "
            f"edge {d.get('edge_pos',0):.2f}  pocD {d.get('poc_dist',0):.1f}  "
            f"one_sided {setup.one_sided:.2f}  squeeze {d.get('squeeze_frac',0):.0%}",
            transform=ax.transAxes, va="top", color="#bbb", fontsize=8, family="monospace")
    ax.tick_params(colors="#888")

    # right: volume profile over the lookback, horizontal
    centers, mass = rl.volume_profile(c[i - lookback:i], v[i - lookback:i],
                                      lo=l[i - lookback:i].min(),
                                      hi=h[i - lookback:i].max(), bins=120)
    if len(centers):
        axv.barh(centers, mass, height=(centers[1] - centers[0]) if len(centers) > 1 else 1,
                 color="#5c6bc0", alpha=0.7)
        axv.axhline(L, color="#ffd54f", linewidth=1.2, linestyle="--")
    axv.set_title("volume profile", color="#888", fontsize=9)
    axv.tick_params(colors="#888")
    plt.tight_layout()
    fig.savefig(out_path, dpi=92, facecolor="#15151f")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--lookback", type=int, default=480)
    p.add_argument("--base-bars", type=int, default=120)
    p.add_argument("--min-touches", type=int, default=3)
    p.add_argument("--one-sided-min", type=float, default=0.75)
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--air-max", type=float, default=0.6,
                   help="max vol-beyond/vol-inside ratio (void on run side)")
    p.add_argument("--edge-band", type=float, default=0.30,
                   help="level must sit in extreme edge_band of the base range")
    p.add_argument("--min-vol-at-level", type=float, default=0.04,
                   help="volume floor at the level (reject thin extrema)")
    p.add_argument("--min-poc-dist-atr", type=float, default=1.5,
                   help="min distance (ATR) from the volume POC; reject magnets")
    p.add_argument("--no-squeeze", action="store_true", help="disable squeeze requirement")
    p.add_argument("--horizon-ms", type=int, default=900_000)
    p.add_argument("--good-mfe", type=float, default=0.01)
    p.add_argument("--dump", type=Path, required=True)
    p.add_argument("--charts", type=Path, default=None)
    p.add_argument("--n-charts", type=int, default=16)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    store = Path(args.store)
    recs, chart_jobs = [], []
    for sym in args.symbols:
        bars, tape_by_date = [], {}
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(store, sym, date, "trades"):
                continue
            t_ts, t_pr, t_qty, _ = mc.load_trades_arr(store, sym, date)
            if len(t_ts) == 0:
                continue
            bars.extend(mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms))
            tape_by_date[date] = (t_ts, t_pr)
        setups = rl.detect_setups(
            bars, lookback=args.lookback, base_bars=args.base_bars,
            min_touches=args.min_touches, one_sided_min=args.one_sided_min,
            min_score=args.min_score, require_squeeze=not args.no_squeeze,
            air_max=args.air_max, edge_band=args.edge_band,
            min_vol_at_level=args.min_vol_at_level,
            min_poc_dist_atr=args.min_poc_dist_atr)
        kept = 0
        for s in setups:
            ts_close = bars[s.idx]["ts"] + args.bar_ms
            date = datetime.fromtimestamp(ts_close / 1000, timezone.utc).strftime("%Y-%m-%d")
            tp = tape_by_date.get(date)
            if tp is None:
                continue
            mv = bs.measure_move(tp[0], tp[1], ts_close, s.side, args.horizon_ms)
            if mv is None:
                continue
            entry, mfe, mae, t_peak = mv
            good = int(mfe >= args.good_mfe)
            recs.append({"symbol": sym, "date": date, "ts_close": ts_close,
                         "side": s.side, "level": s.level, "entry": entry,
                         "mfe": mfe, "mae": mae, "t_peak_ms": t_peak, "good": good,
                         "score": s.score, "one_sided": s.one_sided, **s.detail})
            if args.charts:
                chart_jobs.append((sym, bars, s, mfe, mae, good))
            kept += 1
        print(f"[{sym}] setups={kept} good={sum(1 for r in recs if r['symbol']==sym and r['good'])}",
              flush=True)

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
    print(f"  ROBUST SETUPS  n={n}  good(MFE>={args.good_mfe:.1%})={g} ({g/n:.1%})" if n else "  none")
    if n:
        mfes = sorted(r["mfe"] for r in recs)
        print(f"  MFE median={mfes[n//2]:.2%} p90={mfes[int(n*.9)]:.2%} max={mfes[-1]:.2%}")
        print(f"  dumped {n} -> {args.dump}")

    if args.charts and chart_jobs:
        args.charts.mkdir(parents=True, exist_ok=True)
        random.seed(args.seed)
        runners = [j for j in chart_jobs if j[5]]
        fakes = [j for j in chart_jobs if not j[5]]
        random.shuffle(runners); random.shuffle(fakes)
        pick = runners[:args.n_charts // 2] + fakes[:args.n_charts - args.n_charts // 2]
        for sym, bars, s, mfe, mae, good in pick:
            t = datetime.fromtimestamp(bars[s.idx]["ts"] / 1000, timezone.utc).strftime("%m%d_%H%M")
            tag = "runner" if good else "fake"
            render(bars, s, mfe, mae, good, args.charts / f"{sym}_{t}_{s.side}_{tag}.png",
                   args.lookback)
        print(f"  rendered {len(pick)} charts -> {args.charts}")


if __name__ == "__main__":
    main()
