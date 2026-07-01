"""Robustness check for L=h55 S=h51 exit rule.

Tests:
1. Bootstrap confidence intervals (10k resamples)
2. Permutation test (is L/S split real or random?)
3. Subperiod stability (3 periods)
4. Leave-one-year-out
5. Monte Carlo: random exit hour assignment
6. Effect size & power analysis
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.validate_weekend_macro import load_macro_data, build_weekend_dataset
from scripts.research.weekend_mfe_trailing import augment_dataset_with_missing_predictors

DATA = ROOT / "data" / "processed" / "candles"
PREDICTORS = ["china_inet_fri", "japan_fri", "tech_week", "energy_week", "usdjpy_week"]
MAJORITY = 3
COST_PCT = 0.15
N_BOOT = 10_000
RNG = np.random.default_rng(42)


def load_btc_4h():
    df = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    if pd.api.types.is_numeric_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def get_signal(row, available):
    votes = 0
    n_valid = 0
    for c in available:
        val = row.get(c, np.nan)
        if pd.isna(val):
            continue
        n_valid += 1
        votes += 1 if val > 0 else -1
    if n_valid < MAJORITY or abs(votes) < MAJORITY:
        return None, 0
    return ("long" if votes > 0 else "short"), (n_valid + abs(votes)) // 2


def build_trades(btc, ds):
    available = [c for c in PREDICTORS if c in ds.columns]
    MAX_H = 64
    trades = []
    for _, row in ds.iterrows():
        direction, consensus = get_signal(row, available)
        if direction is None:
            continue
        fri_date = row.get("fri_date")
        entry_ts = pd.Timestamp(fri_date).replace(hour=21, minute=0)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")
        end_ts = entry_ts + pd.Timedelta(hours=MAX_H)
        candles = btc[(btc.index >= entry_ts) & (btc.index <= end_ts)]
        if len(candles) < 5:
            continue
        entry_price = candles.iloc[0]["close"]
        hourly_pnl = {}
        for _, c in candles.iterrows():
            h = int(round((c.name - entry_ts).total_seconds() / 3600))
            if h < 0 or h >= MAX_H:
                continue
            if direction == "long":
                hourly_pnl[h] = (c["close"] - entry_price) / entry_price * 100
            else:
                hourly_pnl[h] = (entry_price - c["close"]) / entry_price * 100
        trades.append({
            "date": entry_ts,
            "year": entry_ts.year,
            "direction": direction,
            "hourly_pnl": hourly_pnl,
        })
    return trades


def pnl_at(trade, h_target):
    if not trade["hourly_pnl"]:
        return np.nan
    best_h = min(trade["hourly_pnl"].keys(), key=lambda h: abs(h - h_target))
    return trade["hourly_pnl"][best_h]


def sharpe(pnls, freq):
    pnls = np.asarray(pnls)
    if len(pnls) < 3 or pnls.std() == 0:
        return 0.0
    return pnls.mean() / pnls.std() * sqrt(freq)


def get_pnls(trades, strategy="baseline"):
    """Get PnL array for a strategy."""
    out = []
    for t in trades:
        if strategy == "baseline":
            out.append(pnl_at(t, 51) - COST_PCT)
        elif strategy == "l55_s51":
            eh = 55 if t["direction"] == "long" else 51
            out.append(pnl_at(t, eh) - COST_PCT)
        elif strategy == "h55":
            out.append(pnl_at(t, 55) - COST_PCT)
    return np.array(out)


def main():
    print("=" * 75)
    print("ROBUSTNESS CHECK: L=h55 S=h51 vs BASELINE (h=51)")
    print("=" * 75)

    btc = load_btc_4h()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc, macro)
    ds = augment_dataset_with_missing_predictors(ds)
    trades = build_trades(btc, ds)

    n = len(trades)
    n_long = sum(1 for t in trades if t["direction"] == "long")
    n_short = n - n_long
    years = (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)
    freq = n / years

    print(f"\nTrades: {n} (long={n_long}, short={n_short}), {years:.1f} years, freq={freq:.1f}/yr")

    base_pnls = get_pnls(trades, "baseline")
    strat_pnls = get_pnls(trades, "l55_s51")
    diff_pnls = strat_pnls - base_pnls  # per-trade improvement

    base_sh = sharpe(base_pnls, freq)
    strat_sh = sharpe(strat_pnls, freq)

    print(f"\nBaseline Sharpe:  {base_sh:+.3f}")
    print(f"L55/S51 Sharpe:   {strat_sh:+.3f}")
    print(f"Delta Sharpe:     {strat_sh - base_sh:+.3f}")
    print(f"\nBaseline avg:     {base_pnls.mean():+.3f}%")
    print(f"L55/S51 avg:      {strat_pnls.mean():+.3f}%")
    print(f"Avg improvement:  {diff_pnls.mean():+.3f}%")

    # ================================================================
    # 1. BOOTSTRAP CI
    # ================================================================
    print(f"\n{'='*75}")
    print(f"1. BOOTSTRAP CONFIDENCE INTERVALS ({N_BOOT} resamples)")
    print(f"{'='*75}")

    boot_base_sh = []
    boot_strat_sh = []
    boot_delta_sh = []
    boot_delta_avg = []

    for _ in range(N_BOOT):
        idx = RNG.integers(0, n, size=n)
        b = base_pnls[idx]
        s = strat_pnls[idx]
        sh_b = sharpe(b, freq)
        sh_s = sharpe(s, freq)
        boot_base_sh.append(sh_b)
        boot_strat_sh.append(sh_s)
        boot_delta_sh.append(sh_s - sh_b)
        boot_delta_avg.append(s.mean() - b.mean())

    boot_delta_sh = np.array(boot_delta_sh)
    boot_delta_avg = np.array(boot_delta_avg)

    for ci_level in [90, 95]:
        lo = np.percentile(boot_delta_sh, (100 - ci_level) / 2)
        hi = np.percentile(boot_delta_sh, 100 - (100 - ci_level) / 2)
        print(f"\n  Sharpe delta {ci_level}% CI: [{lo:+.3f}, {hi:+.3f}]")
        if lo > 0:
            print(f"    >> SIGNIFICANT at {ci_level}% level (CI does not contain 0)")
        else:
            print(f"    >> NOT significant at {ci_level}% level (CI contains 0)")

    lo_avg = np.percentile(boot_delta_avg, 2.5)
    hi_avg = np.percentile(boot_delta_avg, 97.5)
    print(f"\n  Avg PnL delta 95% CI: [{lo_avg:+.3f}%, {hi_avg:+.3f}%]")

    # Prob that strategy is better
    prob_better = (boot_delta_sh > 0).mean()
    print(f"\n  P(strategy Sharpe > baseline): {prob_better:.1%}")

    # Bootstrap Sharpe CIs for each
    print(f"\n  Baseline Sharpe 95% CI: [{np.percentile(boot_base_sh, 2.5):+.3f}, "
          f"{np.percentile(boot_base_sh, 97.5):+.3f}]")
    print(f"  L55/S51 Sharpe 95% CI:  [{np.percentile(boot_strat_sh, 2.5):+.3f}, "
          f"{np.percentile(boot_strat_sh, 97.5):+.3f}]")

    # ================================================================
    # 2. PERMUTATION TEST
    # ================================================================
    print(f"\n{'='*75}")
    print(f"2. PERMUTATION TEST: is the L/S exit split real?")
    print(f"   Shuffle direction labels, recompute Sharpe delta")
    print(f"{'='*75}")

    observed_delta = strat_sh - base_sh
    perm_deltas = []
    directions = [t["direction"] for t in trades]

    for _ in range(N_BOOT):
        # Shuffle direction labels
        shuffled = RNG.permutation(directions)
        perm_pnls = []
        for i, t in enumerate(trades):
            eh = 55 if shuffled[i] == "long" else 51
            perm_pnls.append(pnl_at(t, eh) - COST_PCT)
        perm_sh = sharpe(np.array(perm_pnls), freq)
        perm_deltas.append(perm_sh - base_sh)

    perm_deltas = np.array(perm_deltas)
    p_value = (perm_deltas >= observed_delta).mean()
    print(f"\n  Observed Sharpe delta: {observed_delta:+.3f}")
    print(f"  Permutation p-value:  {p_value:.4f}")
    if p_value < 0.05:
        print(f"  >> SIGNIFICANT (p < 0.05): L/S split is not random")
    else:
        print(f"  >> NOT significant (p >= 0.05): could be random")

    # ================================================================
    # 3. SUBPERIOD STABILITY
    # ================================================================
    print(f"\n{'='*75}")
    print(f"3. SUBPERIOD STABILITY (3 equal periods)")
    print(f"{'='*75}")

    third = n // 3
    periods = [
        ("P1", trades[:third]),
        ("P2", trades[third:2*third]),
        ("P3", trades[2*third:]),
    ]

    n_improved = 0
    print(f"\n  {'Period':<8} {'Dates':>25} {'Base Sh':>9} {'Strat Sh':>9} {'Delta':>8} {'Consistent?':>12}")
    print(f"  {'-'*75}")

    for label, subset in periods:
        f = len(subset) / ((subset[-1]["date"] - subset[0]["date"]).total_seconds() / (365.25 * 86400))
        b = get_pnls(subset, "baseline")
        s = get_pnls(subset, "l55_s51")
        sh_b = sharpe(b, f)
        sh_s = sharpe(s, f)
        d = sh_s - sh_b
        consistent = "YES" if d > 0 else "NO"
        if d > 0:
            n_improved += 1
        dates = f"{subset[0]['date'].strftime('%Y-%m')}..{subset[-1]['date'].strftime('%Y-%m')}"
        print(f"  {label:<8} {dates:>25} {sh_b:>+8.2f} {sh_s:>+8.2f} {d:>+7.3f} {consistent:>12}")

    print(f"\n  Improved in {n_improved}/3 subperiods")

    # ================================================================
    # 4. LEAVE-ONE-YEAR-OUT
    # ================================================================
    print(f"\n{'='*75}")
    print(f"4. LEAVE-ONE-YEAR-OUT")
    print(f"{'='*75}")

    all_years = sorted(set(t["year"] for t in trades))
    n_improved_yr = 0

    print(f"\n  {'Dropped':>8} {'N_rem':>6} {'Base Sh':>9} {'Strat Sh':>9} {'Delta':>8} {'Stable?':>8}")
    print(f"  {'-'*55}")

    for yr in all_years:
        subset = [t for t in trades if t["year"] != yr]
        if len(subset) < 10:
            continue
        f = len(subset) / ((subset[-1]["date"] - subset[0]["date"]).total_seconds() / (365.25 * 86400))
        b = get_pnls(subset, "baseline")
        s = get_pnls(subset, "l55_s51")
        sh_b = sharpe(b, f)
        sh_s = sharpe(s, f)
        d = sh_s - sh_b
        stable = "YES" if d > 0 else "NO"
        if d > 0:
            n_improved_yr += 1
        print(f"  {yr:>8} {len(subset):>6} {sh_b:>+8.2f} {sh_s:>+8.2f} {d:>+7.3f} {stable:>8}")

    print(f"\n  Strategy better in {n_improved_yr}/{len(all_years)} LOO folds")

    # ================================================================
    # 5. MONTE CARLO: random exit hours
    # ================================================================
    print(f"\n{'='*75}")
    print(f"5. MONTE CARLO: random exit hour per direction")
    print(f"   Can random L/S exit assignment beat baseline by +{observed_delta:.3f}?")
    print(f"{'='*75}")

    candidate_hours = [35, 39, 43, 47, 51, 55, 59]
    mc_deltas = []

    for _ in range(N_BOOT):
        hl = RNG.choice(candidate_hours)
        hs = RNG.choice(candidate_hours)
        mc_pnls = []
        for t in trades:
            eh = hl if t["direction"] == "long" else hs
            mc_pnls.append(pnl_at(t, eh) - COST_PCT)
        mc_sh = sharpe(np.array(mc_pnls), freq)
        mc_deltas.append(mc_sh - base_sh)

    mc_deltas = np.array(mc_deltas)
    p_mc = (mc_deltas >= observed_delta).mean()
    print(f"\n  Observed delta: {observed_delta:+.3f}")
    print(f"  P(random L/S assignment >= observed): {p_mc:.4f}")
    print(f"  Median random delta: {np.median(mc_deltas):+.3f}")
    print(f"  95th percentile: {np.percentile(mc_deltas, 95):+.3f}")

    # ================================================================
    # 6. EFFECT SIZE & SAMPLE SIZE
    # ================================================================
    print(f"\n{'='*75}")
    print(f"6. EFFECT SIZE & POWER ANALYSIS")
    print(f"{'='*75}")

    # Only longs are affected (shorts stay at h=51)
    long_trades = [t for t in trades if t["direction"] == "long"]
    long_base = np.array([pnl_at(t, 51) - COST_PCT for t in long_trades])
    long_strat = np.array([pnl_at(t, 55) - COST_PCT for t in long_trades])
    long_diff = long_strat - long_base

    print(f"\n  Longs only (the group that changes): n={len(long_trades)}")
    print(f"  Avg improvement: {long_diff.mean():+.3f}%")
    print(f"  Std of improvement: {long_diff.std():.3f}%")

    cohens_d = long_diff.mean() / long_diff.std() if long_diff.std() > 0 else 0
    print(f"  Cohen's d: {cohens_d:.3f}")
    if abs(cohens_d) < 0.2:
        print(f"    >> SMALL effect (d < 0.2)")
    elif abs(cohens_d) < 0.5:
        print(f"    >> SMALL-MEDIUM effect (0.2 < d < 0.5)")
    elif abs(cohens_d) < 0.8:
        print(f"    >> MEDIUM effect (0.5 < d < 0.8)")
    else:
        print(f"    >> LARGE effect (d >= 0.8)")

    # Paired t-test
    t_stat = long_diff.mean() / (long_diff.std() / sqrt(len(long_diff)))
    # Approximate p-value using normal for large n
    from scipy import stats as scipy_stats
    p_paired = 2 * (1 - scipy_stats.t.cdf(abs(t_stat), df=len(long_diff)-1))
    print(f"\n  Paired t-test: t={t_stat:.3f}, p={p_paired:.4f}")

    # How many trades needed for 80% power?
    if cohens_d > 0:
        # n = (z_alpha + z_beta)^2 / d^2, two-sided alpha=0.05, beta=0.20
        n_needed = int(np.ceil((1.96 + 0.84)**2 / cohens_d**2))
        print(f"\n  Trades needed for 80% power: {n_needed}")
        print(f"  Currently have: {len(long_trades)} longs")
        if len(long_trades) >= n_needed:
            print(f"  >> SUFFICIENT sample size")
        else:
            shortfall = n_needed - len(long_trades)
            weeks_needed = shortfall / (freq * n_long / n)
            print(f"  >> UNDERPOWERED: need {shortfall} more long trades (~{weeks_needed:.0f} more weeks)")

    # ================================================================
    # 7. YEAR-BY-YEAR BREAKDOWN
    # ================================================================
    print(f"\n{'='*75}")
    print(f"7. YEAR-BY-YEAR: does the pattern hold each year?")
    print(f"{'='*75}")

    print(f"\n  {'Year':>6} {'#L':>4} {'#S':>4} {'Base':>9} {'L55/S51':>9} "
          f"{'Delta':>8} {'L@51':>8} {'L@55':>8} {'L delta':>8}")
    print(f"  {'-'*70}")

    for yr in all_years:
        yr_trades = [t for t in trades if t["year"] == yr]
        yr_longs = [t for t in yr_trades if t["direction"] == "long"]
        yr_shorts = [t for t in yr_trades if t["direction"] == "short"]

        b = sum(pnl_at(t, 51) - COST_PCT for t in yr_trades)
        s = sum((pnl_at(t, 55 if t["direction"] == "long" else 51) - COST_PCT) for t in yr_trades)

        l51 = sum(pnl_at(t, 51) - COST_PCT for t in yr_longs) if yr_longs else 0
        l55 = sum(pnl_at(t, 55) - COST_PCT for t in yr_longs) if yr_longs else 0

        print(f"  {yr:>6} {len(yr_longs):>4} {len(yr_shorts):>4} {b:>+8.1f}% {s:>+8.1f}% "
              f"{s-b:>+7.1f}% {l51:>+7.1f}% {l55:>+7.1f}% {l55-l51:>+7.1f}%")

    # ================================================================
    # VERDICT
    # ================================================================
    print(f"\n{'='*75}")
    print(f"VERDICT")
    print(f"{'='*75}")

    checks = {
        "Bootstrap 95% CI excludes 0": np.percentile(boot_delta_sh, 2.5) > 0,
        "Permutation p < 0.05": p_value < 0.05,
        "All 3 subperiods improved": n_improved == 3,
        "All LOO folds improved": n_improved_yr == len(all_years),
        "Monte Carlo p < 0.05": p_mc < 0.05,
        "Paired t-test p < 0.05": p_paired < 0.05,
        "Sufficient sample size": len(long_trades) >= n_needed if cohens_d > 0 else False,
    }

    passed = sum(checks.values())
    total = len(checks)

    for check, ok in checks.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {check}")

    print(f"\n  Score: {passed}/{total}")
    if passed >= 5:
        print(f"  >> ROBUST: deploy with confidence")
    elif passed >= 3:
        print(f"  >> MARGINAL: consider deploying but monitor closely")
    else:
        print(f"  >> WEAK: not enough evidence, keep baseline")

    print(f"\n{'='*75}")
    print("DONE")
    print(f"{'='*75}")


if __name__ == "__main__":
    main()
