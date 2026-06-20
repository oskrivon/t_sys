"""Render breakout setups for eyeball validation: are these really
'consolidation + clear level + iceberg-at-the-wall' situations?

Two panels per setup:
  TOP   price candles — consolidation box, the level, the breakout bar, the
        post-break path, with MFE/MAE and structural features annotated.
  BOTTOM order book AT the level over time — resting notional (the wall) and
        cumulative eaten (trades through it), so we can SEE whether a wall was
        there, got eaten or pulled, and refilled (iceberg).

    python scripts/research/icebreaker_chart_setups.py \
        --store /root/trading/data/icebreaker --cache data/ib_micro.jsonl \
        --out data/charts/setups --n-random 3 --n-top 3
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


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")
es = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")


def level_size_series(surv, level, is_long, t0, t1, tol):
    """Total resting notional within tol of the level on the barrier side, over time."""
    s_ts, s_pr, s_qty, s_side = surv
    bside = mc.ASK if is_long else mc.BID
    m = (s_side == bside) & (s_ts >= t0) & (s_ts <= t1) & (np.abs(s_pr - level) <= level * tol)
    ts, pr, qty = s_ts[m], s_pr[m], s_qty[m]
    o = np.argsort(ts, kind="stable")
    cur, out_t, out_v = {}, [], []
    for t, p, q in zip(ts[o], pr[o], qty[o]):
        cur[round(float(p), 8)] = float(q)
        out_t.append(t)
        out_v.append(sum(pp * qq for pp, qq in cur.items()))
    return np.array(out_t), np.array(out_v)


def eaten_series(trades, level, is_long, t0, t1, tol):
    t_ts, t_pr, t_qty, t_side = trades
    eat = mc.BUY if is_long else mc.SELL
    m = (t_side == eat) & (t_ts >= t0) & (t_ts <= t1) & (np.abs(t_pr - level) <= level * tol)
    ts = t_ts[m]
    o = np.argsort(ts, kind="stable")
    return ts[o], np.cumsum((t_pr[m] * t_qty[m])[o])


def render(bars, surv, trades, b, out_path, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bar_ms = cfg["bar_ms"]
    idx = next((i for i, bar in enumerate(bars) if bar["ts"] == b["ts_close"] - bar_ms), None)
    if idx is None:
        return False
    is_long = b["side"] == "long"
    level = b["level"]
    before = cfg.get("view_before", 24)
    after = cfg.get("view_after", 24)
    lo, hi = max(0, idx - before), min(len(bars), idx + after)
    chunk = bars[lo:hi]

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(13, 9), facecolor="#15151f",
                                  gridspec_kw={"height_ratios": [3, 2]})

    # ---- TOP: price ----
    ax.set_facecolor("#15151f")
    for i, bar in enumerate(chunk):
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        col = "#26a69a" if c >= o else "#ef5350"
        ax.bar(i, abs(c - o), bottom=min(o, c), width=0.6, color=col, edgecolor=col)
        ax.plot([i, i], [l, h], color=col, linewidth=0.8)
    ax.axhline(level, color="#ffd54f", linewidth=1.4, linestyle="--", label=f"level {level:g}")
    # consolidation box
    cs, ce = max(0, idx - cfg["consol_bars"]) - lo, idx - lo
    cwin = bars[max(0, idx - cfg["consol_bars"]):idx]
    if cwin:
        rh = max(x["high"] for x in cwin); rl = min(x["low"] for x in cwin)
        ax.add_patch(plt.Rectangle((cs - 0.5, rl), ce - cs, rh - rl,
                                   color="#5c6bc0", alpha=0.18))
    ax.axvline(idx - lo, color="#42a5f5", linewidth=1.3, linestyle=":")
    entry = b.get("entry", level)
    ax.axhline(entry * (1 + (1 if is_long else -1) * b["mfe"]), color="#26a69a",
               linewidth=0.8, linestyle=":", alpha=0.7, label=f"MFE {b['mfe']:.2%}")
    ax.axhline(entry * (1 - (1 if is_long else -1) * b["mae"]), color="#ef5350",
               linewidth=0.8, linestyle=":", alpha=0.7, label=f"MAE {b['mae']:.2%}")
    t = datetime.fromtimestamp(b["ts_close"] / 1000, timezone.utc).strftime("%m-%d %H:%M")
    verdict = "RUNNER" if b.get("good") else "fakeout"
    ax.set_title(f"{b['symbol']} {t}UTC  {b['side'].upper()}  [{verdict}]   "
                 f"MFE {b['mfe']:.2%} / MAE {b['mae']:.2%}",
                 color="#eee", fontsize=12)
    line1 = f"touches {b['touches']}"
    if "width" in b:
        line1 += f"  width {b['width']:.2%}  brk {b['brk_strength']:.2%}  vol_burst {b['vol_burst']:.1f}"
    if "span_bars" in b:
        line1 += (f"  span {b['span_bars']}bars  lookback {b.get('lookback_bars','?')}"
                  f"  one_sided {b.get('one_sided','?')}")
    txt = (line1 + "\n"
           + (f"wall ${b['wall_notional']:,.0f}  through ${b['through_depth']:,.0f}  "
              f"eaten ${b['eaten']:,.0f}  cancel:eat {b['cancel_eat']:.1f}  "
              f"absorb {b['absorbed_frac']:.2f}  refills {b['refills']}"
              if "wall_notional" in b else "(book panel below)"))
    ax.text(0.01, 0.99, txt, transform=ax.transAxes, va="top", ha="left",
            color="#bbb", fontsize=8.5, family="monospace")
    ax.legend(loc="lower right", fontsize=8, facecolor="#222", labelcolor="#ccc")
    ax.tick_params(colors="#888")

    # ---- BOTTOM: book at the level ----
    ax2.set_facecolor("#15151f")
    t0, t1 = b["ts_close"] - 10 * 60_000, b["ts_close"] + 5 * 60_000
    st, sv = level_size_series(surv, level, is_long, t0, t1, cfg["tol"])
    et, ev = eaten_series(trades, level, is_long, t0, t1, cfg["tol"])
    rel = lambda a: (a - b["ts_close"]) / 60_000.0
    if len(st):
        ax2.step(rel(st), sv, where="post", color="#ffd54f", linewidth=1.3,
                 label="resting $ at level (wall)")
    if len(et):
        ax2.plot(rel(et), ev, color="#26a69a", linewidth=1.3, label="cumulative eaten $")
    ax2.axvline(0, color="#42a5f5", linewidth=1.3, linestyle=":")
    ax2.set_xlabel("minutes from breakout", color="#888")
    ax2.set_ylabel("notional $", color="#888")
    ax2.legend(loc="upper left", fontsize=8, facecolor="#222", labelcolor="#ccc")
    ax2.tick_params(colors="#888")

    plt.tight_layout()
    fig.savefig(out_path, dpi=95, facecolor="#15151f")
    plt.close(fig)
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--n-random", type=int, default=3)
    p.add_argument("--n-top", type=int, default=3)
    p.add_argument("--n-good", type=int, default=2, help="how many RUNNER setups to include")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--bar-ms", type=int, default=300_000)
    p.add_argument("--consol-bars", type=int, default=12)
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--band", type=float, default=0.005)
    p.add_argument("--view-before", type=int, default=24, help="bars shown before break")
    p.add_argument("--view-after", type=int, default=24, help="bars shown after break")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    recs = [json.loads(l) for l in open(args.cache) if l.strip()]
    random.seed(args.seed)
    if recs and "width" in recs[0]:
        es.within_coin_score(recs)
        top = sorted(recs, key=lambda r: r["score"], reverse=True)[:args.n_top]
    else:                                   # major dump: no width/brk -> rank by touches
        top = sorted(recs, key=lambda r: r.get("touches", 0), reverse=True)[:args.n_top]
    runners = [r for r in recs if r.get("good") and r not in top]
    random.shuffle(runners)
    goods = runners[:args.n_good]                                      # show some runners
    chosen = top + goods
    pool = [r for r in recs if r not in chosen]
    chosen += random.sample(pool, min(args.n_random, len(pool)))

    cfg = {"bar_ms": args.bar_ms, "consol_bars": args.consol_bars,
           "tol": args.tol, "band": args.band,
           "view_before": args.view_before, "view_after": args.view_after}
    store = Path(args.store)
    by_day = {}
    for r in chosen:
        by_day.setdefault((r["symbol"], r["date"]), []).append(r)

    done = 0
    for (sym, date), setups in by_day.items():
        trades = mc.load_trades_arr(store, sym, date)
        t_ts, t_pr, t_qty, _ = trades
        bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)
        surv = mc.filter_book(store, sym, date, [s["level"] for s in setups], args.band)
        for r in setups:
            tag = "runner" if r.get("good") else "fake"
            tm = datetime.fromtimestamp(r["ts_close"] / 1000, timezone.utc).strftime("%H%M")
            name = f"{sym}_{date}_{tm}_{r['side']}_{tag}.png"
            if render(bars, surv, trades, r, args.out / name, cfg):
                done += 1
                print(f"saved {name}", flush=True)
    print(f"\n{done} charts -> {args.out}")


if __name__ == "__main__":
    main()
