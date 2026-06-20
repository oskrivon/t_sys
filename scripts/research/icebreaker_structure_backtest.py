"""Phase 0: does a STRUCTURE filter rescue the (dead) wall-absorption signal?

The naive icebreaker — "wall ≥$N vanished + trades absorbed it → enter through" —
was sub-fee / anti-predictive (see docs/ICEBREAKER_RESEARCH.md). The new hypothesis:
the edge concentrates when the wall sits at a *structural level* after a
consolidation (with or without a squeeze), because that is where stops cluster —
eating the wall triggers the stop cascade, not a random fill.

This script reuses the existing absorption detector + TP/SL simulator unchanged,
builds 1m/5m bars from the same trade tape, tags every signal with geometric
structure (level / consolidation / edge / squeeze), and OPTIONALLY scores the
chart with Claude Vision (the screener funnel: chart_generator -> VisionScorer).
It then splits PnL by gate so we can see, cheaply, whether structure helps.

    python scripts/research/icebreaker_structure_backtest.py \
        --store /root/trading/data/icebreaker --symbols TAOUSDT ZECUSDT SUIUSDT \
        --start 2026-03-01 --end 2026-03-31 --bar-ms 300000 [--vision]

Decision rule (Phase 0): if the geo-gated / vision-high subset is not both
materially higher-WR AND closer to net-positive than the ungated set, the
structure idea is no better than the dead naive one — stop here.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from bisect import bisect_right
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Reuse the faithful detector/simulator (scripts/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "icebreaker_signal_backtest",
    ROOT / "scripts" / "research" / "icebreaker_signal_backtest.py")
sbt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sbt
_spec.loader.exec_module(sbt)


# ----------------------------------------------------------------------------
# Bars from the trade tape
# ----------------------------------------------------------------------------
def bars_from_trades(rows, bar_ms):
    """Build OHLCV bars from time-sorted (ts_ms, price, qty) trades.

    Pure + testable. Caller must pass trades sorted by ts so open/close are
    correct; high/low/volume are order-independent.
    """
    buckets: dict[int, dict] = {}
    for ts, px, qty in rows:
        b = (ts // bar_ms) * bar_ms
        bar = buckets.get(b)
        if bar is None:
            buckets[b] = {"ts": b, "open": px, "high": px, "low": px,
                          "close": px, "volume": qty}
        else:
            if px > bar["high"]:
                bar["high"] = px
            if px < bar["low"]:
                bar["low"] = px
            bar["close"] = px
            bar["volume"] += qty
    return [buckets[k] for k in sorted(buckets)]


def load_bar_trades(store, symbol, date):
    """Time-sorted (ts, price, qty) for the day across all trade part files."""
    ts_all, px_all, qty_all = [], [], []
    for f in sbt._parts(store, symbol, date, "trades"):
        t = pq.read_table(f, columns=["exch_ts_ms", "price", "qty"])
        d = t.to_pydict()
        ts_all.extend(d["exch_ts_ms"])
        px_all.extend(d["price"])
        qty_all.extend(d["qty"])
    order = sorted(range(len(ts_all)), key=lambda i: ts_all[i])
    return [(ts_all[i], px_all[i], qty_all[i]) for i in order]


def build_bars(store, symbol, date, bar_ms):
    return bars_from_trades(load_bar_trades(store, symbol, date), bar_ms)


# ----------------------------------------------------------------------------
# Structure tagging (geometric)
# ----------------------------------------------------------------------------
def bar_index_at(bars, ts):
    """Index of the last bar whose bucket start <= ts (-1 if none)."""
    return bisect_right([b["ts"] for b in bars], ts) - 1


def tag_structure(bars, end_ts, price, side, *, lookback_bars, consol_bars,
                  tol, range_thresh, min_touches, squeeze_ratio, edge_tol):
    """Tag a signal's structural context. side=='ask' -> long breakout (resistance).

    - at_level: price was tested >= min_touches times within `tol` over lookback.
    - consolidation: range over the consol_bars before the breakout <= range_thresh.
    - at_edge: the wall sits at the breakout edge of that range (within edge_tol).
    - squeeze: range of the recent half < squeeze_ratio * the earlier half (поджатие).
    """
    tags = dict(at_level=False, consolidation=False, at_edge=False, squeeze=False,
                n_touches=0, range_pct=None, bar_idx=bar_index_at(bars, end_ts))
    idx = tags["bar_idx"]
    if idx < 0:
        return tags
    is_long = (side == "ask")

    window = bars[max(0, idx - lookback_bars):idx + 1]
    if len(window) < max(3, consol_bars // 2):
        return tags
    touches = sum(1 for b in window
                  if abs((b["high"] if is_long else b["low"]) - price) / price <= tol)
    tags["n_touches"] = touches
    tags["at_level"] = touches >= min_touches

    cwin = bars[max(0, idx - consol_bars):idx]   # bars before the breakout bar
    if cwin:
        hi = max(b["high"] for b in cwin)
        lo = min(b["low"] for b in cwin)
        tags["range_pct"] = (hi - lo) / price
        tags["consolidation"] = tags["range_pct"] <= range_thresh
        edge = hi if is_long else lo
        tags["at_edge"] = abs(price - edge) / price <= edge_tol
        if len(cwin) >= 4:
            h = len(cwin) // 2
            early = (max(b["high"] for b in cwin[:h]) - min(b["low"] for b in cwin[:h]))
            recent = (max(b["high"] for b in cwin[h:]) - min(b["low"] for b in cwin[h:]))
            tags["squeeze"] = early > 0 and recent <= squeeze_ratio * early
    return tags


def geo_gated(tags):
    return tags["at_level"] and tags["consolidation"] and tags["at_edge"]


# ----------------------------------------------------------------------------
# Vision tagging (optional) — same funnel as the screener
# ----------------------------------------------------------------------------
def _bars_to_df(bars):
    import pandas as pd
    df = pd.DataFrame(bars)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df


DEFAULT_VISION_MODEL = "anthropic/claude-sonnet-4.6"


def score_chart(png, symbol, is_long, tf, api_key, model):
    """Score one chart via OpenRouter (stdlib urllib — no openai dep on host).

    Returns an int 1-10 or None on any failure. Reuses the screener's prompt.
    """
    import base64
    import json as _json
    import re
    import urllib.request
    from src.ai.vision_scorer import PROMPT_TEMPLATE

    direction = "LONG (buy)" if is_long else "SHORT (sell)"
    prompt = PROMPT_TEMPLATE.format(tf=tf, symbol=symbol,
                                    signal_type="icebreaker", direction=direction)
    b64 = base64.b64encode(png).decode()
    body = {"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": prompt}]}]}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=_json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = _json.loads(r.read())
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return _json.loads(text).get("score")
        except _json.JSONDecodeError:
            m = re.search(r'"score"\s*:\s*(\d+)', text)
            return int(m.group(1)) if m else None
    except Exception as e:
        print(f"    vision error: {e}")
        return None


def score_day(bars, signals, tags_by_id, symbol, tf, api_key, model):
    """Render + score every signal of one day. Returns {id(sig): score|None}."""
    from src.ai.chart_generator import generate_chart
    df = _bars_to_df(bars)
    out = {}
    for s in signals:
        idx = tags_by_id[id(s)]["bar_idx"]
        if idx < 0:
            continue
        is_long = (s.side == "ask")
        png = generate_chart(df, idx, [s.price], "icebreaker", is_long,
                             candles_before=60, candles_after=20)
        if png:
            out[id(s)] = score_chart(png, symbol, is_long, tf, api_key, model)
    return out


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def summarize(name, rows, fee):
    """rows: list of (label, pnl, tags, vscore)."""
    n = len(rows)
    if not n:
        print(f"  {name:24s} n=   0")
        return
    gross = sum(r[1] for r in rows)
    wins = sum(1 for r in rows if r[1] > 0)
    net = gross - n * fee
    lab = Counter(r[0] for r in rows)
    print(f"  {name:24s} n={n:4d}  WR={wins/n:5.1%}  "
          f"gross={gross*100:+7.2f}%  net={net*100:+7.2f}%  "
          f"[tp={lab.get('tp',0)} sl={lab.get('sl',0)} open={lab.get('open',0)}]")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    # absorption signal (passthrough to find_signals/simulate)
    p.add_argument("--min-notional", type=float, default=100_000.0)
    p.add_argument("--absorb-frac", type=float, default=1.0)
    p.add_argument("--tp", type=float, default=0.005)
    p.add_argument("--sl", type=float, default=0.0015)
    p.add_argument("--entry-delay-ms", type=int, default=200)
    p.add_argument("--horizon-ms", type=int, default=60_000)
    p.add_argument("--fee", type=float, default=0.0011)
    # structure tagging
    p.add_argument("--bar-ms", type=int, default=300_000, help="bar size (5m default)")
    p.add_argument("--lookback-bars", type=int, default=48)
    p.add_argument("--consol-bars", type=int, default=12)
    p.add_argument("--tol", type=float, default=0.0015, help="level touch tolerance")
    p.add_argument("--range-thresh", type=float, default=0.02, help="max consol range")
    p.add_argument("--min-touches", type=int, default=2)
    p.add_argument("--squeeze-ratio", type=float, default=0.7)
    p.add_argument("--edge-tol", type=float, default=0.003)
    # vision
    p.add_argument("--vision", action="store_true", help="score each setup with Claude Vision")
    p.add_argument("--vision-model", default="")
    p.add_argument("--vision-thr", type=int, default=7)
    p.add_argument("--dump", type=Path, default=None,
                   help="write signals+tags+pnl+vscore to JSON Lines (the iteration cache)")
    args = p.parse_args()

    rows = []          # (label, pnl, tags, vscore)
    vis_scores = {}    # id(sig) -> score
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if args.vision and not api_key:
        print("WARN: --vision set but OPENROUTER_API_KEY missing; skipping vision")
        args.vision = False

    tf = f"{args.bar_ms // 60000}m"
    for symbol in args.symbols:
        for date in sbt.daterange(args.start, args.end):
            if not sbt._parts(args.store, symbol, date, "book_diff"):
                continue
            ts_s, px_s, by_price = sbt.load_trades(args.store, symbol, date)
            sigs = sbt.find_signals(sbt.iter_book_diffs(args.store, symbol, date),
                                    by_price, args.min_notional, args.absorb_frac)
            if not sigs:
                print(f"[{symbol} {date}] signals=0")
                continue
            bars = build_bars(args.store, symbol, date, args.bar_ms)
            tags_by_id = {
                id(s): tag_structure(
                    bars, s.end_ts, s.price, s.side,
                    lookback_bars=args.lookback_bars, consol_bars=args.consol_bars,
                    tol=args.tol, range_thresh=args.range_thresh,
                    min_touches=args.min_touches, squeeze_ratio=args.squeeze_ratio,
                    edge_tol=args.edge_tol)
                for s in sigs}

            if args.vision:
                model = args.vision_model or DEFAULT_VISION_MODEL
                vis_scores.update(score_day(bars, sigs, tags_by_id, symbol,
                                            tf, api_key, model))

            res = sbt.simulate(sigs, ts_s, px_s, args.tp, args.sl,
                               args.entry_delay_ms, args.horizon_ms)
            for s, label, pnl in res:
                rows.append((label, pnl, tags_by_id[id(s)], vis_scores.get(id(s)),
                             symbol, date, s.side, s.price, s.end_ts))
            ng = sum(1 for s in sigs if geo_gated(tags_by_id[id(s)]))
            print(f"[{symbol} {date}] signals={len(res)} geo_gated={ng}")

    print("\n" + "=" * 78)
    print(f"  ICEBREAKER STRUCTURE BACKTEST  (bar={tf}, min_notional=${args.min_notional:,.0f}, "
          f"TP {args.tp:.2%}/SL {args.sl:.2%}, fee {args.fee:.2%})")
    print("=" * 78)
    summarize("ALL (ungated)", rows, args.fee)
    summarize("at_level", [r for r in rows if r[2]["at_level"]], args.fee)
    summarize("consolidation", [r for r in rows if r[2]["consolidation"]], args.fee)
    summarize("at_edge", [r for r in rows if r[2]["at_edge"]], args.fee)
    summarize("GEO-GATED (lvl+cons+edge)", [r for r in rows if geo_gated(r[2])], args.fee)
    summarize("  + squeeze", [r for r in rows if geo_gated(r[2]) and r[2]["squeeze"]], args.fee)
    if args.vision:
        scored = [r for r in rows if r[3] is not None]
        summarize(f"vision >= {args.vision_thr}",
                  [r for r in scored if r[3] >= args.vision_thr], args.fee)
        summarize(f"vision >= {args.vision_thr} & geo",
                  [r for r in scored if r[3] >= args.vision_thr and geo_gated(r[2])],
                  args.fee)
        print(f"\n  vision scored {len(scored)}/{len(rows)} signals")

    if args.dump:
        import json
        with open(args.dump, "w") as f:
            for label, pnl, tags, vscore, sym, date, side, price, end_ts in rows:
                rec = {"symbol": sym, "date": date, "side": side, "price": price,
                       "end_ts": end_ts, "label": label, "pnl": pnl,
                       "vscore": vscore}
                rec.update(tags)
                f.write(json.dumps(rec) + "\n")
        print(f"  dumped {len(rows)} signals -> {args.dump}")


if __name__ == "__main__":
    main()
