"""Selectivity curve on the alive cell: does tighter selection (further out the mom tail
within CLEAN) raise net/trade and push MORE months net-positive — the customer's idea that
picking the best setups cuts the cost drag and lifts the average win?

Reads per-setup dumps (ib_momdump_{FEB,MAR,APR,MAY}.jsonl) from icebreaker_mom_gate --dump.
Each row: sym, month, mom, clean, gross_trail, net_trail, gross_tib, net_tib, mfe.
fee_units recovered as (gross-net)/0.00055 -> lets us re-price any taker fee tier.

For a grid of "keep top-q by mom within CLEAN (per-month percentile, vol-robust)", reports
pooled net/gross/WR/n + #months net>0, at VIP0 and VIP3 taker. Plus a no-CLEAN arm.

    python scripts/research/icebreaker_selectivity.py
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILE_TAGS = ["FEB", "MAR", "APR", "MAY"]
MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05"]  # overwritten from data in main()
FEE0 = 0.00055                       # VIP0 taker per side (dump baseline)
FEE_TIERS = {"VIP0": 0.00055, "VIP3": 0.00032, "VIP6": 0.00025}
EXIT = "trail"                        # net_trail / gross_trail
KEEP_QS = [1.00, 0.66, 0.50, 0.33, 0.25, 0.15, 0.10]


def load(paths):
    rows = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            print(f"MISSING {p}")
            continue
        for line in open(p):
            if line.strip():
                rows.append(json.loads(line))
    # recover fee_units for re-pricing
    g, n = f"gross_{EXIT}", f"net_{EXIT}"
    for r in rows:
        r["fee_units"] = round((r[g] - r[n]) / FEE0, 4)
        r["gross"] = r[g]
    return rows


def net_at(r, fee_side):
    return r["gross"] - fee_side * r["fee_units"]


def stats(rs, fee_side):
    if not rs:
        return None
    nets = [net_at(r, fee_side) for r in rs]
    n = len(nets)
    mpos = 0
    by_m = defaultdict(list)
    for r, nt in zip(rs, nets):
        by_m[r["month"]].append(nt)
    for m in MONTHS:
        v = by_m.get(m)
        if v and sum(v) / len(v) > 0:
            mpos += 1
    coins = defaultdict(float)
    for r, nt in zip(rs, nets):
        coins[r["sym"]] += nt
    cplus = sum(1 for v in coins.values() if v > 0)
    return {"n": n, "net": sum(nets) / n, "gross": sum(r["gross"] for r in rs) / n,
            "wr": sum(1 for x in nets if x > 0) / n, "mpos": mpos,
            "cplus": cplus, "ncoins": len(coins)}


def select_topq(rows, q, clean_only):
    """Keep top-q fraction by mom WITHIN each month (per-month percentile)."""
    pool = [r for r in rows if (r["clean"] if clean_only else True)]
    if q >= 1.0:
        return pool
    keep = []
    bym = defaultdict(list)
    for r in pool:
        bym[r["month"]].append(r)
    for m, rs in bym.items():
        rs = sorted(rs, key=lambda r: r["mom"], reverse=True)
        k = max(1, int(round(len(rs) * q)))
        keep.extend(rs[:k])
    return keep


def main():
    global MONTHS
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", default=[
        str(ROOT / "data" / f"ib_momdump_{m}.jsonl") for m in FILE_TAGS])
    ap.add_argument("--coin-curve", action="store_true",
                    help="also print per-coin net at the q=0.15 operating point")
    args = ap.parse_args()
    rows = load(args.files)
    MONTHS = sorted(set(r["month"] for r in rows))
    print(f"loaded {len(rows)} setups ({EXIT} exit), {len(MONTHS)} months "
          f"{MONTHS[0]}..{MONTHS[-1]}\n")

    for clean_only in (False, True):
        tag = "CLEAN" if clean_only else "ALL"
        print(f"================ population: {tag} ================")
        print(f"  {'keep_q':>7s} {'n':>5s} {'net0%':>7s} {'netV3%':>7s} {'gross%':>7s} "
              f"{'WR':>4s} {'mon+':>5s} {'coins+':>7s}")
        for q in KEEP_QS:
            rs = select_topq(rows, q, clean_only)
            s0 = stats(rs, FEE_TIERS["VIP0"])
            s3 = stats(rs, FEE_TIERS["VIP3"])
            if not s0:
                continue
            print(f"  {q:7.2f} {s0['n']:5d} {s0['net']*100:+7.3f} {s3['net']*100:+7.3f} "
                  f"{s0['gross']*100:+7.3f} {s0['wr']*100:3.0f}% {s0['mpos']:>3d}/{len(MONTHS)} "
                  f"{s0['cplus']:>2d}/{s0['ncoins']:<2d}")
        print()

    # detailed per-month for the most selective CLEAN cells
    print("================ per-month detail: CLEAN x top-q mom ================")
    for q in [0.50, 0.33, 0.25, 0.15]:
        rs = select_topq(rows, q, True)
        bym = defaultdict(list)
        for r in rs:
            bym[r["month"]].append(r)
        print(f"\n-- CLEAN top-{q:.0%} mom --")
        for m in MONTHS:
            s = stats(bym.get(m, []), FEE_TIERS["VIP0"])
            if s:
                print(f"   {m}: n={s['n']:<3} net0={s['net']*100:+.3f}% "
                      f"gross={s['gross']*100:+.3f}% WR={s['wr']*100:.0f}% "
                      f"coins+={s['cplus']}/{s['ncoins']}")

    if args.coin_curve:
        print("\n================ per-coin net at CLEAN top-15% mom (VIP0) ================")
        rs = select_topq(rows, 0.15, True)
        bycoin = defaultdict(list)
        for r in rs:
            bycoin[r["sym"]].append(r)
        for sym in sorted(bycoin, key=lambda s: -sum(net_at(r, FEE0) for r in bycoin[s])):
            v = bycoin[sym]
            tot = sum(net_at(r, FEE0) for r in v)
            mean = tot / len(v)
            wr = sum(1 for r in v if net_at(r, FEE0) > 0) / len(v)
            print(f"   {sym:<14} n={len(v):<3} net={mean*100:+.3f}% sum={tot*100:+.1f}% WR={wr*100:.0f}%")


if __name__ == "__main__":
    main()
