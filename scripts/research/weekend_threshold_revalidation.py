"""Re-validation of the Weekend entry threshold.

Production (`src/weekend/ensemble.py`, majority_threshold=3) trades on a simple
COUNT majority: >=3 of 5 predictors agree. The validation that confirmed the
edge used a NET margin rule: |vote_sum| >= 3, i.e. >=4 of 5 agree.

This script tests whether tightening to the validated 4-of-5 rule is justified:

  1. Head-to-head: net 4of5 vs count 3of5 (N, WR, avg, Sharpe + bootstrap CI)
  2. MARGINAL trades (taken by 3of5 but NOT 4of5): do they have any edge?
     -> if their mean return CI includes/sits at <=0, the loose rule adds noise.
  3. Robustness of the 4of5 rule: bootstrap Sharpe CI, subperiods, LOO, perm.

Exit modeled = production: Sun 12:00 UTC with 5% catastrophe stop-loss.
Run:  python scripts/research/weekend_threshold_revalidation.py
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.validate_weekend_macro import (
    load_btc_4h, load_macro_data, build_weekend_dataset,
)
from scripts.research.weekend_mfe_trailing import augment_dataset_with_missing_predictors
from scripts.research.weekend_window_corr_backtest import (
    extend_btc_live, build_trades, build_corr_series, COST_PCT,
)

BASE = "Sun12 (prod)"
N_BOOT = 10_000
RNG = np.random.default_rng(42)


def rets(trades):
    """Per-trade net return at production exit (Sun12 + 5% SL), after costs."""
    return np.array([t["pnl_sl"][BASE] - COST_PCT for t in trades
                     if not np.isnan(t["pnl_sl"][BASE])])


def yrs(trades):
    return (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)


def sharpe(a, freq):
    a = np.asarray(a)
    return a.mean() / a.std() * sqrt(freq) if len(a) > 2 and a.std() > 0 else 0.0


def boot_ci(a, freq, stat="sharpe", level=95):
    n = len(a)
    vals = []
    for _ in range(N_BOOT):
        s = a[RNG.integers(0, n, n)]
        vals.append(sharpe(s, freq) if stat == "sharpe" else s.mean())
    lo = np.percentile(vals, (100 - level) / 2)
    hi = np.percentile(vals, 100 - (100 - level) / 2)
    return lo, hi


def main():
    print("=" * 74)
    print("WEEKEND ENTRY-THRESHOLD RE-VALIDATION:  net 4of5  vs  count 3of5 (LIVE)")
    print("=" * 74)

    btc = extend_btc_live(load_btc_4h())
    macro = load_macro_data()
    ds = build_weekend_dataset(btc, macro)
    ds = augment_dataset_with_missing_predictors(ds)
    corr = build_corr_series(btc, macro)

    net = build_trades(btc, ds, corr, "net")     # validated 4-of-5
    cnt = build_trades(btc, ds, corr, "count")   # production 3-of-5
    net_dates = {t["date"] for t in net}
    marginal = [t for t in cnt if t["date"] not in net_dates]  # 3of5-only

    freq_net = len(net) / max(yrs(net), 0.1)
    freq_cnt = len(cnt) / max(yrs(cnt), 0.1)
    freq_mar = len(marginal) / max(yrs(marginal), 0.1)

    rn, rc, rm = rets(net), rets(cnt), rets(marginal)

    # ---- 1. Head to head ----
    print(f"\n{'-'*74}\n1. HEAD-TO-HEAD (exit Sun12 + 5% SL, after costs)\n{'-'*74}")
    print(f"{'Rule':>22} {'N':>4} {'WR%':>6} {'Avg%':>8} {'Total%':>9} {'Sharpe':>8} {'Sharpe 95% CI':>20}")
    for label, r, f in [("net 4of5 (validated)", rn, freq_net),
                        ("count 3of5 (LIVE)", rc, freq_cnt)]:
        lo, hi = boot_ci(r, f, "sharpe")
        print(f"{label:>22} {len(r):>4} {(r>0).mean()*100:>5.0f} {r.mean():>+7.3f} "
              f"{r.sum():>+8.1f} {sharpe(r,f):>+7.2f}   [{lo:+.2f}, {hi:+.2f}]")

    # ---- 2. Marginal trades (the cost of the loose rule) ----
    print(f"\n{'-'*74}\n2. MARGINAL TRADES — taken by 3of5 but NOT 4of5 (n={len(rm)})\n{'-'*74}")
    lo_m, hi_m = boot_ci(rm, freq_mar, "mean")
    sh_lo, sh_hi = boot_ci(rm, freq_mar, "sharpe")
    print(f"   These are the extra weekends the LIVE rule trades and the validated rule skips.")
    print(f"   WR:            {(rm>0).mean()*100:.0f}%")
    print(f"   Avg return:    {rm.mean():+.3f}%   (95% CI [{lo_m:+.3f}, {hi_m:+.3f}])")
    print(f"   Total:         {rm.sum():+.1f}%")
    print(f"   Sharpe:        {sharpe(rm,freq_mar):+.2f}   (95% CI [{sh_lo:+.2f}, {sh_hi:+.2f}])")
    verdict_m = ("NO EDGE — loose rule adds noise/drag" if hi_m <= 0 or rm.mean() <= 0
                 else "ambiguous (CI spans 0)" if lo_m < 0 < hi_m
                 else "positive edge")
    print(f"   >> {verdict_m}")

    # ---- 3. Robustness of the 4of5 rule ----
    print(f"\n{'-'*74}\n3. ROBUSTNESS OF THE 4of5 RULE\n{'-'*74}")
    lo, hi = boot_ci(rn, freq_net, "sharpe")
    mlo, mhi = boot_ci(rn, freq_net, "mean")
    print(f"   Bootstrap Sharpe 95% CI: [{lo:+.2f}, {hi:+.2f}]  ({'excludes 0' if lo>0 else 'INCLUDES 0'})")
    print(f"   Bootstrap mean   95% CI: [{mlo:+.3f}, {mhi:+.3f}]%")

    # subperiods (3 equal by trade count)
    third = len(net) // 3
    subs = [("P1", net[:third]), ("P2", net[third:2*third]), ("P3", net[2*third:])]
    pos = 0
    print(f"\n   Subperiods:")
    for name, sub in subs:
        f = len(sub) / max(yrs(sub), 0.1)
        r = rets(sub)
        sh = sharpe(r, f)
        pos += sh > 0
        d = f"{sub[0]['date'].strftime('%Y-%m')}..{sub[-1]['date'].strftime('%Y-%m')}"
        print(f"     {name} {d:>16}  n={len(r):>3}  WR={(r>0).mean()*100:>3.0f}%  Sharpe={sh:>+5.2f}")
    print(f"     -> positive in {pos}/3 subperiods")

    # leave-one-year-out
    years = sorted({t["date"].year for t in net})
    loo_pos = 0
    print(f"\n   Leave-one-year-out:")
    for yr in years:
        sub = [t for t in net if t["date"].year != yr]
        if len(sub) < 10:
            continue
        f = len(sub) / max(yrs(sub), 0.1)
        sh = sharpe(rets(sub), f)
        loo_pos += sh > 0
        print(f"     drop {yr}:  n={len(sub):>3}  Sharpe={sh:>+5.2f}")
    print(f"     -> positive in {loo_pos}/{len(years)} folds")

    # permutation: shuffle direction signs, is Sharpe real?
    obs = sharpe(rn, freq_net)
    base_dir = np.array([1 if t["direction"] == "long" else -1 for t in net])
    raw = np.array([(t["pnl_sl"][BASE] - COST_PCT) for t in net])  # signed already
    # reconstruct unsigned move to re-sign under permutation
    unsigned = np.array([(t["pnl_sl"][BASE]) for t in net]) * base_dir  # back out raw move
    perm = []
    for _ in range(N_BOOT):
        sh_dir = RNG.permutation(base_dir)
        pr = unsigned * sh_dir - COST_PCT
        perm.append(sharpe(pr, freq_net))
    p_perm = (np.array(perm) >= obs).mean()
    print(f"\n   Permutation (shuffle direction): observed Sharpe={obs:+.2f}, p={p_perm:.4f} "
          f"({'real' if p_perm < 0.05 else 'NOT significant'})")

    # ---- verdict ----
    print(f"\n{'='*74}\nVERDICT\n{'='*74}")
    checks = {
        "4of5 Sharpe CI excludes 0": lo > 0,
        "4of5 positive in 3/3 subperiods": pos == 3,
        "4of5 positive in all LOO folds": loo_pos == len(years),
        "4of5 permutation p<0.05": p_perm < 0.05,
        "marginal 3of5 trades have NO positive edge": rm.mean() <= 0 or hi_m <= 0,
        "4of5 Sharpe > count 3of5 Sharpe": sharpe(rn, freq_net) > sharpe(rc, freq_cnt),
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    score = sum(checks.values())
    print(f"\n  Score: {score}/{len(checks)}")
    print("  >> TIGHTEN to 4of5 (majority_threshold=4)" if score >= 5
          else "  >> INCONCLUSIVE — review before changing")
    print(f"\n{'='*74}\nDONE\n{'='*74}")


if __name__ == "__main__":
    main()
