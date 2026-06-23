"""Vision SELECTION test on liquidity-vacuum breakouts — does a model's eye pick the
fat runners a discretionary scalper would take?

The Phase-1.8 verdict: mechanically the average vacuum-breakout is ~breakeven at taker
fees, and the gates don't rank WHICH break runs (score AUC 0.50). The last open lever
is discretionary selection — a human takes 1-2 of the fattest setups/day. This tests
whether Claude Vision can stand in for that eye.

Honest by construction (unlike Phase 0, which leaked 20 future candles into the chart
AND still failed):
  - render is DECISION-TIME: candles only up to the break bar, level + volume profile,
    NO future bars, NO outcome label, NO gate numbers (pure visual gestalt).
  - vision is asked, blind to outcome, for BOTH a 0-10 "will it run" score AND a
    binary trade/skip decision (+1-line reason) — score for AUC, decision for a
    realized-PnL subset.
  - we then join to the REAL managed-exit PnL (taker, validated 1.7 exit) and ask:
    does score separate runners (AUC vs MFE>=1%), and is the vision-"trade" subset
    net-positive at taker fees where the full population is breakeven?

    OPENROUTER_API_KEY in env or .env. Pilot:
    python scripts/research/icebreaker_vision_select.py \
        --store /root/trading/data/icebreaker_active \
        --cache /root/trading/tmp/ib_liq_empty.jsonl --limit 100 --tag MAR \
        --charts /root/trading/tmp/ib_vis_charts --dump /root/trading/tmp/ib_vision_mar.jsonl
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import random
import re
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


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")
es = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")
from src.strategy import robust_levels as rl

EXIT_CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
            "horizon_ms": 1_800_000, "fee_side": 0.00055}
MODEL = "anthropic/claude-sonnet-4.6"


# ---------------------------------------------------------------------------
# Vision: prompt, call, parse (parse is pure/testable)
# ---------------------------------------------------------------------------
PROMPT = """You are a discretionary crypto futures breakout scalper. This 1-minute \
chart shows price action UP TO THE MOMENT a {direction} breakout of the marked level \
(yellow dashed line) just triggered. You are deciding in real time: there is NO future \
data on the chart. The right panel is the volume profile of the prior range.

Judging ONLY from what is visible, will this breakout RUN in the {direction} direction \
for a sizable move, or just poke through and revert? You only take 1-2 of the very best \
setups per day, so be selective.

Respond ONLY with JSON: {{"score": N, "decision": "trade" or "skip", "reason": "one \
short sentence"}}  where score is 0-10 (10 = textbook high-conviction runner)."""


def parse_vision(text):
    """Extract {score:int, decision:str, reason:str} from a model reply; None on fail."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        d = json.loads(text)
        sc = d.get("score"); dec = d.get("decision")
    except json.JSONDecodeError:
        m = re.search(r'"score"\s*:\s*(\d+)', text)
        sc = int(m.group(1)) if m else None
        m2 = re.search(r'"decision"\s*:\s*"?(trade|skip)"?', text, re.I)
        dec = m2.group(1).lower() if m2 else None
        m3 = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
        d = {"reason": m3.group(1) if m3 else ""}
    if sc is None:
        return None
    dec = str(dec).lower() if dec else ("trade" if int(sc) >= 7 else "skip")
    return {"score": int(sc), "decision": "trade" if dec == "trade" else "skip",
            "reason": str(d.get("reason", ""))[:200]}


def vision_judge(png, side, api_key, model):
    import urllib.request
    direction = "SHORT (downside)" if side == "short" else "LONG (upside)"
    body = {"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url":
            {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
        {"type": "text", "text": PROMPT.format(direction=direction)}]}]}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        return parse_vision(data["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"    vision error: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Decision-time render (no future bars, no outcome)
# ---------------------------------------------------------------------------
def render_decision(bars, bar_ms, rec, out_path, lookback):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    L = rec["level"]
    bts = np.array([b["ts"] for b in bars])
    i = int(np.searchsorted(bts, rec["ts_close"] - bar_ms))      # the break bar
    i = min(max(i, 1), len(bars) - 1)
    c = np.array([b["close"] for b in bars]); v = np.array([b["volume"] for b in bars])
    h = np.array([b["high"] for b in bars]); l = np.array([b["low"] for b in bars])
    lo, hi = max(0, i - 300), i + 1                              # NO future bars
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
    xb = i - lo
    ax.axvline(xb, color="#42a5f5", linewidth=1.0, linestyle=":")
    em = "^" if rec["side"] == "long" else "v"
    ax.plot(xb, float(c[i]), marker=em, color="#ffffff", markersize=11,
            markeredgecolor="#42a5f5", zorder=5)
    t = datetime.fromtimestamp(rec["ts_close"] / 1000, timezone.utc).strftime("%m-%d %H:%M")
    ax.set_title(f"{rec['symbol']} {rec['side'].upper()} {t}UTC — breakout decision point",
                 color="#eee", fontsize=11)
    ax.legend(loc="lower left", fontsize=7, facecolor="#222", labelcolor="#ccc")
    ax.tick_params(colors="#888")

    j0 = max(0, i - lookback)
    centers, mass = rl.volume_profile(c[j0:i], v[j0:i], lo=l[j0:i].min(), hi=h[j0:i].max(),
                                      bins=120)
    if len(centers):
        axv.barh(centers, mass, height=(centers[1] - centers[0]) if len(centers) > 1 else 1,
                 color="#5c6bc0", alpha=0.7)
        axv.axhline(L, color="#ffd54f", linewidth=1.1, linestyle="--")
    axv.set_title("volume profile", color="#888", fontsize=9)
    axv.tick_params(colors="#888")
    fig.tight_layout()
    fig.savefig(out_path, dpi=92, facecolor="#15151f")
    plt.close(fig)


# ---------------------------------------------------------------------------
def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg]); ranks = {}; i = 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[k] = r
        i = j
    rp = sum(ranks[idx] for idx, (_, lab) in enumerate(allv) if lab == 1)
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def load_env_key(env_file=None):
    k = os.getenv("OPENROUTER_API_KEY")
    if k:
        return k
    cands = [Path(env_file)] if env_file else []
    cands += [ROOT / ".env", Path("/root/trading/.env")]
    for env in cands:
        if env and env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def good(rows, gm=0.01):
    return sum(1 for r in rows if r["mfe"] >= gm) / len(rows) if rows else float("nan")


def net_of(r, fee):
    return r["gross"] - fee * r["fee_units"]


def subset_line(name, rows, fm, ft):
    if not rows:
        print(f"  {name:24s} n=   0"); return
    nt = [net_of(r, ft) for r in rows]; nm = [net_of(r, fm) for r in rows]
    per = defaultdict(float)
    for r, x in zip(rows, nt):
        per[r["symbol"]] += x
    print(f"  {name:24s} n={len(rows):4d}  good={good(rows):5.1%}  "
          f"taker={sum(nt)/len(nt)*100:+.3f}%  maker={sum(nm)/len(nm)*100:+.3f}%  "
          f"med_tk={sorted(nt)[len(nt)//2]*100:+.3f}%  win={sum(1 for x in nt if x>0)/len(nt):5.1%}  "
          f"sum_tk={sum(nt)*100:+.1f}%  coins+={sum(1 for x in per.values() if x>0)}/{len(per)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--charts", type=Path, required=True)
    p.add_argument("--dump", type=Path, required=True)
    p.add_argument("--limit", type=int, default=0, help="random sample N setups (0=all)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--lookback", type=int, default=480)
    p.add_argument("--fee-maker", type=float, default=0.0002)
    p.add_argument("--fee-taker", type=float, default=0.00055)
    p.add_argument("--tag", default="")
    p.add_argument("--env-file", default="", help="path to .env with OPENROUTER_API_KEY")
    p.add_argument("--reuse", action="store_true", help="reanalyze existing --dump, no API")
    args = p.parse_args()

    if args.reuse and args.dump.exists():
        scored = [json.loads(l) for l in open(args.dump) if l.strip()]
    else:
        api_key = load_env_key(args.env_file or None)
        if not api_key:
            print("ERROR: OPENROUTER_API_KEY missing (env or .env)"); sys.exit(1)
        recs = [json.loads(l) for l in open(args.cache) if l.strip()]
        if args.limit and len(recs) > args.limit:
            random.seed(args.seed); recs = random.sample(recs, args.limit)
        by_day = defaultdict(list)
        for r in recs:
            by_day[(r["symbol"], r["date"])].append(r)
        args.charts.mkdir(parents=True, exist_ok=True)
        store = Path(args.store)
        scored = []
        done = 0
        for (sym, date), setups in sorted(by_day.items()):
            t_ts, t_pr, t_qty, _ = mc.load_trades_arr(store, sym, date)
            if len(t_ts) == 0:
                continue
            bars = mc.bars_np(t_ts, t_pr, t_qty, args.bar_ms)
            for r in setups:
                ex = es.simulate_exit(t_ts, t_pr, r["ts_close"], r["side"], r["level"], EXIT_CFG)
                if ex is None:
                    continue
                t = datetime.fromtimestamp(r["ts_close"] / 1000, timezone.utc).strftime("%m%d_%H%M")
                png_path = args.charts / f"{sym}_{t}_{r['side']}.png"
                render_decision(bars, args.bar_ms, r, png_path, args.lookback)
                vj = vision_judge(png_path.read_bytes(), r["side"], api_key, args.model)
                if vj is None:
                    continue
                scored.append({"symbol": sym, "date": date, "side": r["side"],
                               "ts_close": r["ts_close"], "level": r["level"],
                               "mfe": r["mfe"], "gross": ex["gross"],
                               "fee_units": ex["fee_units"], "reason_exit": ex["reason"],
                               "vscore": vj["score"], "vdecision": vj["decision"],
                               "vreason": vj["reason"]})
                done += 1
                if done % 10 == 0:
                    print(f"  scored {done}...", flush=True)
        with open(args.dump, "w") as fo:
            for r in scored:
                fo.write(json.dumps(r) + "\n")
        print(f"  dumped {len(scored)} -> {args.dump}", flush=True)

    # ---- analysis ----------------------------------------------------------
    n = len(scored)
    fm, ft = args.fee_maker, args.fee_taker
    print("=" * 100)
    print(f"  VISION SELECT  {args.tag}  n={n}  model={args.model}")
    print("=" * 100)
    if not n:
        return
    pos = [r["vscore"] for r in scored if r["mfe"] >= 0.01]
    neg = [r["vscore"] for r in scored if r["mfe"] < 0.01]
    print(f"  vscore AUC vs (MFE>=1%): {auc(pos, neg):.3f}   "
          f"(base good-rate {good(scored):.1%}, n_good={len(pos)})")
    print(f"  score histogram: " +
          "  ".join(f"{s}:{sum(1 for r in scored if r['vscore']==s)}" for s in range(0, 11)))
    print("\n  -- by score band --")
    for lo_, hi_, nm in [(0, 4, "0-3"), (4, 7, "4-6"), (7, 9, "7-8"), (9, 11, "9-10")]:
        subset_line(f"score {nm}", [r for r in scored if lo_ <= r["vscore"] < hi_], fm, ft)
    print("\n  -- vision decision --")
    subset_line("ALL", scored, fm, ft)
    subset_line("vision TRADE", [r for r in scored if r["vdecision"] == "trade"], fm, ft)
    subset_line("vision SKIP", [r for r in scored if r["vdecision"] == "skip"], fm, ft)
    # trade-decision precision/recall vs runner
    tr = [r for r in scored if r["vdecision"] == "trade"]
    ng = sum(1 for r in scored if r["mfe"] >= 0.01)
    if tr:
        tp = sum(1 for r in tr if r["mfe"] >= 0.01)
        print(f"\n  decision 'trade': n={len(tr)} ({len(tr)/n:.0%} of setups), "
              f"precision(runner)={tp/len(tr):.1%} vs base {good(scored):.1%}, "
              f"recall={tp/ng:.1%}" if ng else "")


if __name__ == "__main__":
    main()
