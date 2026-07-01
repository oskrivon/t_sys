"""Robustness view on the fade dump from icebreaker_fade_sim.py: is the low-mom fade edge
consistent ACROSS MONTHS and COINS (tradeable) or a one-window/one-coin artifact?

Selects the fade universe by mom threshold (fade setups with mom-k0 < --mom-max, the tail the
breakout discards), for a chosen stop-buf, and reports pooled + per-month + per-coin net at
taker and maker, all-coins and CLEAN.

    python scripts/research/icebreaker_fade_analyze.py --dump data/ib_fade_2y.jsonl \
        --stop-buf 0.005 --mom-max 0.006
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict


def net(rec, fee):
    return rec["gross"] - fee * rec["fee_units"]


def line(label, nets):
    if not nets:
        return
    n = len(nets)
    mean = sum(nets) / n
    wr = sum(x > 0 for x in nets) / n * 100
    s = sorted(nets)
    med = s[n // 2]
    print(f"  {label:<12} n={n:4d}  net={mean*100:+.3f}%  WR={wr:3.0f}%  med={med*100:+.3f}%  sum={sum(nets)*100:+.1f}%")


def block(rows, key, taker, maker, title):
    print(f"\n### {title}  (n={len(rows)}) ###")
    line("taker", [net(r["fade"][key], taker) for r in rows])
    line("maker", [net(r["fade"][key], maker) for r in rows])

    print("  -- per month (maker) --")
    bym = defaultdict(list)
    for r in rows:
        bym[r["month"]].append(net(r["fade"][key], maker))
    pos = 0
    for m in sorted(bym):
        nets = bym[m]
        mean = sum(nets) / len(nets)
        pos += mean > 0
        print(f"     {m}  n={len(nets):3d}  maker_net={mean*100:+.3f}%  WR={sum(x>0 for x in nets)/len(nets)*100:3.0f}%")
    print(f"     -> {pos}/{len(bym)} months maker-positive")

    print("  -- per coin (maker) --")
    byc = defaultdict(list)
    for r in rows:
        byc[r["sym"]].append(net(r["fade"][key], maker))
    posc = 0
    for c in sorted(byc, key=lambda x: -sum(byc[x]) / len(byc[x])):
        nets = byc[c]
        mean = sum(nets) / len(nets)
        posc += mean > 0
        print(f"     {c:<16} n={len(nets):3d}  maker_net={mean*100:+.3f}%")
    print(f"     -> {posc}/{len(byc)} coins maker-positive")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--stop-buf", type=float, default=0.005)
    ap.add_argument("--mom-max", type=float, default=0.006, help="fade only setups with mom < this")
    ap.add_argument("--taker", type=float, default=0.00055)
    ap.add_argument("--maker", type=float, default=0.0002)
    args = ap.parse_args()
    key = str(args.stop_buf)

    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    rows = [r for r in rows if key in r["fade"] and r["mom"] < args.mom_max]
    clean = [r for r in rows if r["clean"]]
    print(f"loaded fade universe: mom<{args.mom_max}, stop={args.stop_buf}: "
          f"{len(rows)} setups ({len(clean)} CLEAN)")

    block(rows, key, args.taker, args.maker, f"ALL-coins  mom<{args.mom_max}")
    if clean:
        block(clean, key, args.taker, args.maker, f"CLEAN  mom<{args.mom_max}")


if __name__ == "__main__":
    main()
