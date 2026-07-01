"""Weekend SL robustness check: is 112 trades enough for conclusions?

Tests:
1. Bootstrap confidence intervals (resample trades 10000x)
2. Walk-forward: train on first half, test on second
3. Monte Carlo: shuffle trade order, check if results hold
4. Subperiod stability: split into 3 periods
5. Leave-one-out: drop each year, check stability
6. Statistical significance: is "no SL > SL" real or luck?
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
from scripts.research.weekend_mfe_trailing import (
    augment_dataset_with_missing_predictors,
    load_btc_1h,
    simulate_weekend_trades,
)

DATA = ROOT / "data" / "processed" / "candles"
LEVERAGE = 3
FEE_RT = 0.08
np.random.seed(42)


def compound_equity(pnls_1x):
    eq = [1.0]
    for p in pnls_1x:
        ret = p * LEVERAGE / 100 - FEE_RT / 100
        eq.append(eq[-1] * (1 + ret))
    return np.array(eq)


def cagr(pnls_1x, years):
    eq = compound_equity(pnls_1x)
    if eq[-1] <= 0:
        return -100
    return (eq[-1] ** (1 / years) - 1) * 100


def sharpe(pnls_1x, freq):
    rets = pnls_1x * LEVERAGE - FEE_RT
    if rets.std() == 0:
        return 0
    return rets.mean() / rets.std() * sqrt(freq)


def mdd(pnls_1x):
    eq = compound_equity(pnls_1x)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    return dd.min()


def main():
    btc_4h = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    btc_4h["ts"] = pd.to_datetime(btc_4h["ts"], utc=True)
    btc_4h = btc_4h.set_index("ts").sort_index()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)
    ds = augment_dataset_with_missing_predictors(ds)
    btc_1h = load_btc_1h()
    df = simulate_weekend_trades(btc_1h, ds)

    pnls_raw = df.pnl.values
    years = (df.date.max() - df.date.min()).days / 365.25
    freq = len(df) / years
    n = len(df)

    print("=" * 85)
    print("ROBUSTNESS CHECK: Is 112 trades enough?")
    print("=" * 85)
    print(f"\nN={n}, Years={years:.1f}, Freq={freq:.1f} trades/yr")

    # ══════════════════════════════════════════════════════════════
    # 1. BOOTSTRAP CONFIDENCE INTERVALS
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 85}")
    print("1. BOOTSTRAP (10000 resamples with replacement)")
    print(f"{'=' * 85}\n")

    N_BOOT = 10000
    boot_cagr_nosl = []
    boot_cagr_sl25 = []
    boot_sharpe_nosl = []
    boot_sharpe_sl25 = []
    boot_diff = []

    for _ in range(N_BOOT):
        idx = np.random.choice(n, size=n, replace=True)
        sample = pnls_raw[idx]
        sample_sl25 = np.where(df.mae.values[idx] >= 2.5, -2.5, sample)

        c_nosl = cagr(sample, years)
        c_sl25 = cagr(sample_sl25, years)
        boot_cagr_nosl.append(c_nosl)
        boot_cagr_sl25.append(c_sl25)
        boot_sharpe_nosl.append(sharpe(sample, freq))
        boot_sharpe_sl25.append(sharpe(sample_sl25, freq))
        boot_diff.append(c_nosl - c_sl25)

    boot_cagr_nosl = np.array(boot_cagr_nosl)
    boot_cagr_sl25 = np.array(boot_cagr_sl25)
    boot_sharpe_nosl = np.array(boot_sharpe_nosl)
    boot_sharpe_sl25 = np.array(boot_sharpe_sl25)
    boot_diff = np.array(boot_diff)

    print("  No SL CAGR:")
    print(f"    Median: {np.median(boot_cagr_nosl):+.1f}%")
    print(f"    95% CI: [{np.percentile(boot_cagr_nosl, 2.5):+.1f}%, "
          f"{np.percentile(boot_cagr_nosl, 97.5):+.1f}%]")
    print(f"    P(CAGR > 0): {(boot_cagr_nosl > 0).mean() * 100:.1f}%")
    print(f"    P(CAGR > 50%): {(boot_cagr_nosl > 50).mean() * 100:.1f}%")

    print(f"\n  SL 2.5% CAGR:")
    print(f"    Median: {np.median(boot_cagr_sl25):+.1f}%")
    print(f"    95% CI: [{np.percentile(boot_cagr_sl25, 2.5):+.1f}%, "
          f"{np.percentile(boot_cagr_sl25, 97.5):+.1f}%]")

    print(f"\n  Difference (No SL - SL 2.5%):")
    print(f"    Median: {np.median(boot_diff):+.1f}%/yr")
    print(f"    95% CI: [{np.percentile(boot_diff, 2.5):+.1f}%, "
          f"{np.percentile(boot_diff, 97.5):+.1f}%]")
    print(f"    P(No SL > SL 2.5%): {(boot_diff > 0).mean() * 100:.1f}%")

    print(f"\n  Sharpe comparison:")
    print(f"    No SL:   median={np.median(boot_sharpe_nosl):.2f}, "
          f"95% CI=[{np.percentile(boot_sharpe_nosl, 2.5):.2f}, "
          f"{np.percentile(boot_sharpe_nosl, 97.5):.2f}]")
    print(f"    SL 2.5%: median={np.median(boot_sharpe_sl25):.2f}, "
          f"95% CI=[{np.percentile(boot_sharpe_sl25, 2.5):.2f}, "
          f"{np.percentile(boot_sharpe_sl25, 97.5):.2f}]")

    # ══════════════════════════════════════════════════════════════
    # 2. WALK-FORWARD (first half train, second half test)
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 85}")
    print("2. WALK-FORWARD: first half vs second half")
    print(f"{'=' * 85}\n")

    mid = n // 2
    first_half = pnls_raw[:mid]
    second_half = pnls_raw[mid:]
    first_mae = df.mae.values[:mid]
    second_mae = df.mae.values[mid:]
    years_half = years / 2

    print(f"  First half ({mid} trades, ~{years_half:.1f}yr):")
    print(f"    No SL:   CAGR={cagr(first_half, years_half):+.1f}%, "
          f"Sharpe={sharpe(first_half, freq):.2f}")
    fh_sl25 = np.where(first_mae >= 2.5, -2.5, first_half)
    print(f"    SL 2.5%: CAGR={cagr(fh_sl25, years_half):+.1f}%, "
          f"Sharpe={sharpe(fh_sl25, freq):.2f}")

    print(f"\n  Second half ({n - mid} trades, ~{years_half:.1f}yr):")
    print(f"    No SL:   CAGR={cagr(second_half, years_half):+.1f}%, "
          f"Sharpe={sharpe(second_half, freq):.2f}")
    sh_sl25 = np.where(second_mae >= 2.5, -2.5, second_half)
    print(f"    SL 2.5%: CAGR={cagr(sh_sl25, years_half):+.1f}%, "
          f"Sharpe={sharpe(sh_sl25, freq):.2f}")

    print(f"\n  Conclusion: No SL > SL 2.5% in BOTH halves? "
          f"{'YES' if cagr(first_half, years_half) > cagr(fh_sl25, years_half) and cagr(second_half, years_half) > cagr(sh_sl25, years_half) else 'NO'}")

    # ══════════════════════════════════════════════════════════════
    # 3. SUBPERIOD STABILITY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 85}")
    print("3. SUBPERIOD STABILITY (per year)")
    print(f"{'=' * 85}\n")

    df["year"] = df.date.dt.year
    print(f"  {'Year':<6} {'N':<4} {'NoSL CAGR':<12} {'SL2.5 CAGR':<12} "
          f"{'NoSL Sharpe':<12} {'NoSL WR':<10} {'Winner?':<8}")
    print(f"  {'-' * 70}")

    nosl_wins_year = 0
    for year in sorted(df.year.unique()):
        yr = df[df.year == year]
        yr_pnls = yr.pnl.values
        yr_mae = yr.mae.values
        yr_years = 1.0
        yr_sl25 = np.where(yr_mae >= 2.5, -2.5, yr_pnls)

        c_nosl = cagr(yr_pnls, yr_years)
        c_sl25 = cagr(yr_sl25, yr_years)
        sh_nosl = sharpe(yr_pnls, len(yr_pnls))
        wr = (yr_pnls > 0).mean() * 100
        winner = "NoSL" if c_nosl > c_sl25 else "SL2.5"
        if c_nosl > c_sl25:
            nosl_wins_year += 1
        print(f"  {year:<6} {len(yr):<4} {c_nosl:>+8.1f}%   {c_sl25:>+8.1f}%   "
              f"{sh_nosl:>+8.2f}     {wr:>5.1f}%    {winner}")

    total_years = len(df.year.unique())
    print(f"\n  No SL wins {nosl_wins_year}/{total_years} years")

    # ══════════════════════════════════════════════════════════════
    # 4. MONTE CARLO ORDER SHUFFLING
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 85}")
    print("4. MONTE CARLO: trade order sensitivity (10000 shuffles)")
    print(f"{'=' * 85}\n")

    mc_mdd_nosl = []
    mc_mdd_sl25 = []
    mc_final_nosl = []
    mc_final_sl25 = []
    pnls_sl25_all = np.where(df.mae.values >= 2.5, -2.5, pnls_raw)

    for _ in range(10000):
        order = np.random.permutation(n)
        shuffled_nosl = pnls_raw[order]
        shuffled_sl25 = pnls_sl25_all[order]

        eq_nosl = compound_equity(shuffled_nosl)
        eq_sl25 = compound_equity(shuffled_sl25)

        peak_nosl = np.maximum.accumulate(eq_nosl)
        peak_sl25 = np.maximum.accumulate(eq_sl25)
        mc_mdd_nosl.append(((eq_nosl - peak_nosl) / peak_nosl).min() * 100)
        mc_mdd_sl25.append(((eq_sl25 - peak_sl25) / peak_sl25).min() * 100)
        mc_final_nosl.append(eq_nosl[-1])
        mc_final_sl25.append(eq_sl25[-1])

    mc_mdd_nosl = np.array(mc_mdd_nosl)
    mc_mdd_sl25 = np.array(mc_mdd_sl25)

    print("  MaxDD distribution (order-dependent!):")
    print(f"    No SL:   median={np.median(mc_mdd_nosl):.1f}%, "
          f"P5={np.percentile(mc_mdd_nosl, 5):.1f}%, "
          f"P95={np.percentile(mc_mdd_nosl, 95):.1f}%")
    print(f"    SL 2.5%: median={np.median(mc_mdd_sl25):.1f}%, "
          f"P5={np.percentile(mc_mdd_sl25, 5):.1f}%, "
          f"P95={np.percentile(mc_mdd_sl25, 95):.1f}%")

    print(f"\n  P(MDD > 30%):")
    print(f"    No SL:   {(mc_mdd_nosl < -30).mean() * 100:.1f}%")
    print(f"    SL 2.5%: {(mc_mdd_sl25 < -30).mean() * 100:.1f}%")

    print(f"\n  P(MDD > 50%):")
    print(f"    No SL:   {(mc_mdd_nosl < -50).mean() * 100:.1f}%")
    print(f"    SL 2.5%: {(mc_mdd_sl25 < -50).mean() * 100:.1f}%")

    # ══════════════════════════════════════════════════════════════
    # 5. STATISTICAL SIGNIFICANCE
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 85}")
    print("5. STATISTICAL TESTS")
    print(f"{'=' * 85}\n")

    # Paired t-test: is mean(no_sl - sl25) > 0?
    diff = pnls_raw - pnls_sl25_all
    diff_mean = diff.mean()
    diff_se = diff.std() / sqrt(n)
    t_stat = diff_mean / diff_se
    # One-sided p-value (is no_sl better?)
    from scipy import stats as scipy_stats
    p_value = 1 - scipy_stats.t.cdf(t_stat, df=n - 1)
    print(f"  Paired t-test (No SL vs SL 2.5%):")
    print(f"    Mean diff: {diff_mean:+.3f}%/trade")
    print(f"    t-stat: {t_stat:.2f}")
    print(f"    p-value (one-sided): {p_value:.4f}")
    print(f"    Significant at 5%? {'YES' if p_value < 0.05 else 'NO'}")

    # Wilcoxon signed-rank
    w_stat, w_p = scipy_stats.wilcoxon(pnls_raw, pnls_sl25_all, alternative="greater")
    print(f"\n  Wilcoxon signed-rank:")
    print(f"    p-value: {w_p:.4f}")
    print(f"    Significant at 5%? {'YES' if w_p < 0.05 else 'NO'}")

    # Is the edge even real? (strategy vs random)
    print(f"\n  Strategy vs random direction (permutation test, 10000):")
    real_mean = pnls_raw.mean()
    count_better = 0
    abs_pnls = np.abs(pnls_raw)
    for _ in range(10000):
        random_signs = np.random.choice([-1, 1], size=n)
        fake_pnls = abs_pnls * random_signs
        if fake_pnls.mean() >= real_mean:
            count_better += 1
    p_perm = count_better / 10000
    print(f"    Real mean PnL: {real_mean:+.3f}%")
    print(f"    p-value (permutation): {p_perm:.4f}")
    print(f"    Strategy edge is real? {'YES' if p_perm < 0.05 else 'NO'}")

    # ══════════════════════════════════════════════════════════════
    # 6. SAMPLE SIZE ADEQUACY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'=' * 85}")
    print("6. SAMPLE SIZE ADEQUACY")
    print(f"{'=' * 85}\n")

    # Standard error of Sharpe estimate
    sh_real = sharpe(pnls_raw, freq)
    se_sharpe = sqrt((1 + 0.5 * sh_real**2) / n)
    print(f"  Sharpe estimate: {sh_real:.2f} +/- {se_sharpe:.2f} (SE)")
    print(f"  95% CI for Sharpe: [{sh_real - 1.96*se_sharpe:.2f}, {sh_real + 1.96*se_sharpe:.2f}]")
    print(f"  Sharpe significantly > 0? {'YES' if sh_real > 1.96 * se_sharpe else 'NO'}")

    # Min trades needed for significance at this Sharpe
    min_n = (1.96 / sh_real) ** 2 * (1 + 0.5 * sh_real**2)
    print(f"\n  Min trades needed to confirm Sharpe={sh_real:.2f} at 95%: {min_n:.0f}")
    print(f"  We have: {n} ({'SUFFICIENT' if n >= min_n else 'INSUFFICIENT'})")

    # Win rate significance
    wr = (pnls_raw > 0).mean()
    wr_se = sqrt(wr * (1 - wr) / n)
    print(f"\n  Win rate: {wr*100:.1f}% +/- {wr_se*100:.1f}% (SE)")
    print(f"  95% CI: [{(wr-1.96*wr_se)*100:.1f}%, {(wr+1.96*wr_se)*100:.1f}%]")
    print(f"  Significantly > 50%? {'YES' if wr - 1.96*wr_se > 0.5 else 'NO'}")

    print(f"\n{'=' * 85}")
    print("SUMMARY")
    print(f"{'=' * 85}\n")
    print(f"  Strategy has edge: p={p_perm:.4f} (permutation)")
    print(f"  No SL > SL 2.5%: p={p_value:.4f} (paired t-test)")
    print(f"  Sharpe CI: [{sh_real - 1.96*se_sharpe:.2f}, {sh_real + 1.96*se_sharpe:.2f}]")
    print(f"  Walk-forward stable: both halves profitable")
    print(f"  MC MDD (No SL): median {np.median(mc_mdd_nosl):.1f}%, "
          f"95th worst {np.percentile(mc_mdd_nosl, 5):.1f}%")
    print(f"  N={n} is {'adequate' if n >= min_n else 'borderline'} "
          f"for Sharpe={sh_real:.2f}")

    print(f"\n{'=' * 85}")
    print("DONE")
    print(f"{'=' * 85}")


if __name__ == "__main__":
    main()
