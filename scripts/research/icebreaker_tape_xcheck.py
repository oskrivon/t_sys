"""Tape cross-check of the deployed fixed rule (CLEAN & mom-k0 >= thr) -- removes the klines
bar-fill assumption that the scorecard relies on. Compares, APPLES-TO-APPLES on the SAME 9
memes x SAME 4 months (Feb-May 2026), the per-trade net from:
  - TAPE  : real trade-tape managed-exit dumps (ib_momdump_{FEB,MAR,APR,MAY}.jsonl)
  - KLINES: the 1m bar-walk dump used by the scorecard (default ib_momdump_3y.jsonl)

The scorecard charged klines a flat -5bps haircut for bar-fill optimism. This script measures
that optimism directly (klines_net - tape_net) on the overlap and checks the TAPE rule is
net-positive on its own -- i.e. the certified edge survives without the klines assumption.

    python scripts/research/icebreaker_tape_xcheck.py
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAPE_TAGS = ["FEB", "MAR", "APR", "MAY"]
THRS = [0.02, 0.025, 0.03]
EXIT = "trail"


def load(paths):
    rows = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            print(f"MISSING {p}"); continue
        for line in open(p):
            if line.strip():
                rows.append(json.loads(line))
    return rows


def stats(rows, thr, months, coins):
    sel = [r for r in rows if r["clean"] and r["mom"] >= thr]
    if not sel:
        return None
    nets = [r[f"net_{EXIT}"] for r in sel]
    gross = [r[f"gross_{EXIT}"] for r in sel]
    n = len(nets)
    by_m = defaultdict(list)
    for r, nt in zip(sel, nets):
        by_m[r["month"]].append(nt)
    mpos = sum(1 for m in months if by_m.get(m) and sum(by_m[m]) / len(by_m[m]) > 0)
    by_c = defaultdict(float)
    for r, nt in zip(sel, nets):
        by_c[r["sym"]] += nt
    cplus = sum(1 for v in by_c.values() if v > 0)
    return {"n": n, "net": sum(nets) / n, "gross": sum(gross) / n,
            "wr": sum(1 for x in nets if x > 0) / n, "sum": sum(nets),
            "mpos": mpos, "cplus": cplus, "ncoins": len(by_c), "by_m": by_m}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape-files", nargs="+",
                    default=[str(ROOT / "data" / f"ib_momdump_{m}.jsonl") for m in TAPE_TAGS])
    ap.add_argument("--klines-dump", default=str(ROOT / "data" / "ib_momdump_3y.jsonl"))
    args = ap.parse_args()

    tape = load(args.tape_files)
    coins = sorted({r["sym"] for r in tape})
    months = sorted({r["month"] for r in tape})
    print(f"TAPE: {len(tape)} setups | {len(coins)} coins | months {months[0]}..{months[-1]}")

    # restrict klines to the SAME coins x months for an apples-to-apples comparison
    kl_all = load([args.klines_dump])
    kl = [r for r in kl_all if r["sym"] in set(coins) and r["month"] in set(months)]
    print(f"KLINES (restricted to same 9 coins x {len(months)} months): "
          f"{len(kl)}/{len(kl_all)} setups\n")

    hdr = (f"  {'thr':>5s} | {'src':>6s} {'n':>4s} {'net%':>7s} {'gross%':>7s} {'WR':>4s} "
           f"{'sum%':>7s} {'mon+':>5s} {'coins+':>7s}")
    for thr in THRS:
        st = stats(tape, thr, months, coins)
        sk = stats(kl, thr, months, coins)
        print(hdr if thr == THRS[0] else "")
        if st:
            print(f"  {thr:5.1%} | {'TAPE':>6s} {st['n']:4d} {st['net']*100:+7.3f} "
                  f"{st['gross']*100:+7.3f} {st['wr']*100:3.0f}% {st['sum']*100:+7.1f} "
                  f"{st['mpos']:>3d}/{len(months)} {st['cplus']:>2d}/{st['ncoins']:<2d}")
        if sk:
            print(f"  {thr:5.1%} | {'KLINE':>6s} {sk['n']:4d} {sk['net']*100:+7.3f} "
                  f"{sk['gross']*100:+7.3f} {sk['wr']*100:3.0f}% {sk['sum']*100:+7.1f} "
                  f"{sk['mpos']:>3d}/{len(months)} {sk['cplus']:>2d}/{sk['ncoins']:<2d}")
        if st and sk:
            gap = (sk["net"] - st["net"]) * 100
            print(f"  {'':5s} | gap (klines optimism) = {gap:+.3f}pp/trade  "
                  f"(scorecard haircut = +0.050pp)")

    # per-month tape detail at the deployed threshold
    print("\n================ TAPE per-month @ mom>=2.5% (CLEAN) ================")
    st = stats(tape, 0.025, months, coins)
    for m in months:
        v = st["by_m"].get(m, [])
        if v:
            print(f"   {m}: n={len(v):<3} net={sum(v)/len(v)*100:+.3f}% "
                  f"WR={sum(1 for x in v if x>0)/len(v)*100:.0f}%")


if __name__ == "__main__":
    main()
