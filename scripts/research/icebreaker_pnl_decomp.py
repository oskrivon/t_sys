"""What eats the PnL? — decompose a managed-exit dump.

Phase 1.9 left a paradox: median net is solidly positive (+0.3%) but mean is ~0. That
means a minority of large losses cancels the many small gains. This reads a dump that
carries per-setup gross / fee_units / reason_exit / mfe (e.g. the vision dumps) and
breaks the realized taker PnL down by:
  - exit reason (stop / trail / horizon): count, mean, sum, share of total
  - win/loss shape: win-rate, avg win, avg loss, payoff ratio, expectancy
  - the tail: how much the worst-K trades cost, and the mean without them
  - runner capture: for MFE>=1% setups, captured gross vs the MFE that was on offer
    (how much of a real runner the exit actually banks)

    python scripts/research/icebreaker_pnl_decomp.py \
        --dumps '/root/trading/tmp/ib_vision_{mar_full,apr}.jsonl'
"""
from __future__ import annotations

import argparse
import glob
import json


def decomp(name, rs, fee, worst_k=10):
    for r in rs:
        r["net"] = r["gross"] - fee * r["fee_units"]
    n = len(rs)
    if not n:
        print(f"  {name}: empty"); return
    nets = sorted(r["net"] for r in rs)
    tot = sum(r["net"] for r in rs)
    print("=" * 74)
    print(f"  {name}  n={n}  total_net={tot*100:+.1f}%  mean={tot/n*100:+.3f}%  "
          f"median={nets[n//2]*100:+.3f}%")

    print("  --- by exit reason ---")
    for reason in ("stop", "trail", "horizon"):
        b = [r for r in rs if r.get("reason_exit") == reason]
        if not b:
            continue
        s = sum(r["net"] for r in b)
        print(f"    {reason:8s} n={len(b):4d} ({len(b)/n:4.0%})  mean={s/len(b)*100:+.3f}%  "
              f"sum={s*100:+6.1f}%  ({(s/tot*100 if tot else 0):+5.0f}% of total)  "
              f"mfe_avg={sum(x['mfe'] for x in b)/len(b)*100:.2f}%")

    wins = [r["net"] for r in rs if r["net"] > 0]
    losses = [r["net"] for r in rs if r["net"] <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    print(f"  --- win/loss ---  WR={len(wins)/n:.0%}  avgWin={aw*100:+.3f}%  "
          f"avgLoss={al*100:+.3f}%  payoff={abs(aw/al) if al else 0:.2f}  "
          f"expectancy={(len(wins)/n*aw + len(losses)/n*al)*100:+.3f}%")

    worst = nets[:worst_k]
    ws = sum(worst)
    print(f"  --- tail ---  worst-{worst_k} sum={ws*100:+.1f}%  "
          f"(={(ws/tot*100 if tot else 0):+.0f}% of total)  "
          f"mean_without_them={(tot-ws)/(n-worst_k)*100:+.3f}%")

    run = [r for r in rs if r["mfe"] >= 0.01]
    if run:
        capt = sum(r["gross"] for r in run) / len(run)
        mfe = sum(r["mfe"] for r in run) / len(run)
        print(f"  --- runner capture (MFE>=1%, n={len(run)}) ---  avg_MFE={mfe*100:.2f}%  "
              f"avg_captured_gross={capt*100:+.3f}%  capture_ratio={capt/mfe*100 if mfe else 0:.0f}%")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dumps", required=True, help="glob of dump(s) with gross/fee_units/reason_exit/mfe")
    p.add_argument("--fee", type=float, default=0.00055, help="round-trip taker is 2x this")
    p.add_argument("--label", default="")
    args = p.parse_args()
    files = sorted(glob.glob(args.dumps))
    if not files:
        print(f"no files match {args.dumps}"); return
    for fp in files:
        rs = [json.loads(l) for l in open(fp) if l.strip()]
        decomp(args.label or fp.split("/")[-1], rs, args.fee)


if __name__ == "__main__":
    main()
