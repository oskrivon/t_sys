"""Is the axis the per-setup ABSOLUTE momentum magnitude (coin-agnostic), not a coin
attribute? Bin all CLEAN setups by |mom-k0| (universe-wide, absolute), report net/gross/WR
+ how many distinct coins each bin spans. If net rises with |mom| and high-|mom| bins profit
across MANY coins -> the edge is mom-magnitude, selectable by an absolute threshold (a clean
operating rule), not a per-coin/per-month percentile.

    python scripts/research/icebreaker_mombin.py --dump data/ib_momdump_wide.jsonl
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

FEE_V3 = 0.00032


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--clean-only", action="store_true", default=True)
    args = ap.parse_args()
    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    rows = [r for r in rows if r["clean"]]
    for r in rows:
        r["fee_units"] = (r["gross_trail"] - r["net_trail"]) / 0.00055

    # absolute mom bins (signed mom is already in break direction; >0 = with-break thrust)
    edges = [-1, 0, 0.003, 0.006, 0.010, 0.015, 0.025, 0.05, 9]
    bins = defaultdict(list)
    for r in rows:
        m = r["mom"]
        for i in range(len(edges) - 1):
            if edges[i] <= m < edges[i + 1]:
                bins[i].append(r); break

    print(f"CLEAN setups={len(rows)}, binned by signed mom-k0 (with-break thrust)\n")
    print(f"  {'mom range':>16} {'n':>5} {'net0%':>7} {'netV3%':>7} {'gross%':>7} "
          f"{'WR':>4} {'coins':>5} {'coins+':>7}")
    for i in range(len(edges) - 1):
        rs = bins.get(i, [])
        if not rs:
            continue
        n = len(rs)
        net0 = sum(r["net_trail"] for r in rs) / n
        netv3 = sum(r["gross_trail"] - FEE_V3 * r["fee_units"] for r in rs) / n
        gross = sum(r["gross_trail"] for r in rs) / n
        wr = sum(1 for r in rs if r["net_trail"] > 0) / n
        bycoin = defaultdict(float)
        for r in rs:
            bycoin[r["sym"]] += r["net_trail"]
        cplus = sum(1 for v in bycoin.values() if v > 0)
        lo, hi = edges[i], edges[i + 1]
        rng = f"[{lo:.3f},{hi:.3f})" if hi < 9 else f">={lo:.3f}"
        print(f"  {rng:>16} {n:>5} {net0*100:+7.3f} {netv3*100:+7.3f} {gross*100:+7.3f} "
              f"{wr*100:3.0f}% {len(bycoin):>5} {cplus:>3}/{len(bycoin):<3}")


if __name__ == "__main__":
    main()
