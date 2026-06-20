"""Fast re-analysis of the Phase-0 signal cache (icebreaker_structure_backtest --dump).

The heavy book-streaming run writes one JSON-Lines record per signal (tags +
realized label/pnl + vision score). This reads that cache and recomputes gate
breakdowns instantly, so we can sweep fee levels (taker/maker), vision
thresholds and gate combos without touching the 8.8GB store again.

    python scripts/research/icebreaker_phase0_analyze.py \
        --cache data/icebreaker_phase0_cache.jsonl --fee 0.0011 --maker 0.0004
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def geo(r):
    return r["at_level"] and r["consolidation"] and r["at_edge"]


def summarize(name, recs, fees):
    """fees: dict label->rate. Prints n / WR / gross / per-trade / net@each fee."""
    n = len(recs)
    if not n:
        print(f"  {name:26s} n=   0")
        return
    gross = sum(r["pnl"] for r in recs)
    wins = sum(1 for r in recs if r["pnl"] > 0)
    lab = Counter(r["label"] for r in recs)
    per = gross / n
    nets = "  ".join(f"{tag}={(gross - n*rate)*100:+6.2f}%" for tag, rate in fees.items())
    print(f"  {name:26s} n={n:4d}  WR={wins/n:5.1%}  gross={gross*100:+7.2f}%  "
          f"edge/trade={per*100:+.3f}%  [tp={lab.get('tp',0)} sl={lab.get('sl',0)} "
          f"open={lab.get('open',0)}]\n  {'':26s} net: {nets}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--fee", type=float, default=0.0011, help="taker round-trip")
    p.add_argument("--maker", type=float, default=0.0004, help="maker round-trip")
    p.add_argument("--vision-thr", type=int, default=7)
    args = p.parse_args()

    recs = load(args.cache)
    fees = {"taker": args.fee, "maker": args.maker}
    thr = args.vision_thr
    scored = [r for r in recs if r.get("vscore") is not None]

    gates = [
        ("ALL (ungated)", lambda r: True),
        ("at_level", lambda r: r["at_level"]),
        ("consolidation", lambda r: r["consolidation"]),
        ("at_edge", lambda r: r["at_edge"]),
        ("level+consol", lambda r: r["at_level"] and r["consolidation"]),
        ("GEO (lvl+cons+edge)", geo),
        ("GEO + squeeze", lambda r: geo(r) and r["squeeze"]),
        (f"vision >= {thr}", lambda r: r.get("vscore") is not None and r["vscore"] >= thr),
        (f"vision >= {thr} & geo", lambda r: r.get("vscore") is not None and r["vscore"] >= thr and geo(r)),
        (f"vision >= {thr} & consol", lambda r: r.get("vscore") is not None and r["vscore"] >= thr and r["consolidation"]),
    ]

    print("=" * 88)
    print(f"  PHASE-0 CACHE ANALYSIS  n={len(recs)}  vision-scored={len(scored)}  "
          f"taker={args.fee:.2%} maker={args.maker:.2%}")
    print("=" * 88)
    for name, fn in gates:
        summarize(name, [r for r in recs if fn(r)], fees)

    # vision threshold sweep (does a higher bar concentrate edge?)
    if scored:
        print("\n  vision threshold sweep (edge/trade, taker-net):")
        for t in range(3, 10):
            sub = [r for r in scored if r["vscore"] >= t]
            if sub:
                g = sum(r["pnl"] for r in sub)
                print(f"    >={t}: n={len(sub):3d}  edge/trade={g/len(sub)*100:+.3f}%  "
                      f"net@taker={(g - len(sub)*args.fee)*100:+6.2f}%")


if __name__ == "__main__":
    main()
