"""Statistical validation: Deflated Sharpe Ratio, PBO, MinBTL.

Implements:
- DSR: Bailey & Lopez de Prado (2014) — adjusts Sharpe for multiple testing + non-normality
- PBO: Bailey, Borwein, Lopez de Prado, Zhu (2015) — probability of backtest overfitting
- MinBTL: Bailey et al. (2014) — minimum backtest length for significance

Usage:
    from src.validation.statistical import deflated_sharpe, pbo, min_btl

    dsr = deflated_sharpe(sharpe=1.5, n_trials=20, n_obs=500, skew=-0.3, kurt=4.0)
    print(f"DSR p-value: {dsr.p_value:.3f}  {'PASS' if dsr.is_significant else 'FAIL'}")

    pbo_result = pbo(returns_matrix)
    print(f"PBO: {pbo_result.pbo:.2f}  {'OVERFIT' if pbo_result.is_overfit else 'OK'}")
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import exp, log, sqrt, pi

import numpy as np
from scipy import stats as sp_stats


# ===========================================================================
# Deflated Sharpe Ratio
# ===========================================================================

@dataclass
class DSRResult:
    """Result of Deflated Sharpe Ratio test."""
    observed_sharpe: float
    deflated_sharpe: float
    expected_max_sharpe: float   # E[max SR] under null given N trials
    p_value: float               # P(SR > observed | null)
    n_trials: int
    n_observations: int
    skewness: float
    kurtosis: float              # excess kurtosis

    @property
    def is_significant(self) -> bool:
        return self.p_value < 0.05


def deflated_sharpe(
    sharpe: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    annualize_factor: float = 365.0,
) -> DSRResult:
    """Compute the Deflated Sharpe Ratio.

    Adjusts the observed Sharpe for:
    1. Number of independent trials (multiple testing)
    2. Non-normality of returns (skew, kurtosis)

    Args:
        sharpe: Observed annualized Sharpe ratio.
        n_trials: Number of strategy configurations tested.
        n_obs: Number of return observations (e.g., daily returns).
        skew: Skewness of returns.
        kurt: Kurtosis of returns (NOT excess — scipy convention).
              Normal = 3.0. Pass raw kurtosis from scipy.stats.kurtosis(fisher=False).
        annualize_factor: Annualization factor used for the Sharpe.
    """
    if n_trials < 1:
        n_trials = 1
    if n_obs < 2:
        return DSRResult(sharpe, sharpe, 0.0, 1.0, n_trials, n_obs, skew, kurt)

    # Work in annualized space (both SR and E[max] annualized)
    sr = sharpe

    # Expected maximum annualized Sharpe under the null given N trials.
    # E[max] of N i.i.d. standard normals ≈ sqrt(2·ln(N)) (for large N).
    # But SR estimator has std ≈ 1/sqrt(n_obs) per period, annualized = sqrt(AF/n_obs).
    # So E[max SR_annual] ≈ sqrt(2·ln(N)) · sqrt(annualize_factor / n_obs)
    e_max_z = _expected_max_z(n_trials)
    sr_std_under_null = sqrt(annualize_factor / n_obs)
    e_max_sr = e_max_z * sr_std_under_null

    # Variance of SR estimator adjusted for non-normality
    # Var(SR_annual) ≈ (1 - skew·sr_pp + (kurt-1)/4·sr_pp²) · (AF/n_obs)
    # where sr_pp = per-period Sharpe
    sr_pp = sr / sqrt(annualize_factor)
    excess_kurt = kurt - 3.0
    var_sr = (1.0 - skew * sr_pp + (excess_kurt / 4.0) * sr_pp**2) * (annualize_factor / (n_obs - 1))
    if var_sr <= 0:
        var_sr = annualize_factor / (n_obs - 1)

    std_sr = sqrt(var_sr)

    # DSR: probability that true SR > E[max SR] given observed SR
    if std_sr > 0:
        z_score = (sr - e_max_sr) / std_sr
        p_value = 1.0 - sp_stats.norm.cdf(z_score)
        deflated = float(z_score)
    else:
        p_value = 0.5
        deflated = 0.0

    return DSRResult(
        observed_sharpe=sharpe,
        deflated_sharpe=deflated,
        expected_max_sharpe=e_max_sr,
        p_value=p_value,
        n_trials=n_trials,
        n_observations=n_obs,
        skewness=skew,
        kurtosis=kurt,
    )


def deflated_sharpe_from_returns(
    returns: np.ndarray,
    n_trials: int,
    annualize_factor: float = 365.0,
) -> DSRResult:
    """Convenience: compute DSR directly from a returns array."""
    if len(returns) < 2:
        return DSRResult(0, 0, 0, 1.0, n_trials, len(returns), 0, 3.0)

    sr_per_period = returns.mean() / returns.std() if returns.std() > 0 else 0.0
    sharpe_annual = sr_per_period * sqrt(annualize_factor)

    skew = float(sp_stats.skew(returns))
    kurt = float(sp_stats.kurtosis(returns, fisher=False))  # raw kurtosis

    return deflated_sharpe(
        sharpe=sharpe_annual,
        n_trials=n_trials,
        n_obs=len(returns),
        skew=skew,
        kurt=kurt,
        annualize_factor=annualize_factor,
    )


# ===========================================================================
# Probability of Backtest Overfitting (PBO)
# ===========================================================================

@dataclass
class PBOResult:
    """Result of Probability of Backtest Overfitting."""
    pbo: float                  # probability of overfitting [0, 1]
    logit_distribution: np.ndarray  # logit values for each combination
    n_combinations: int
    n_strategies: int
    n_subsets: int

    @property
    def is_overfit(self) -> bool:
        return self.pbo > 0.50


def pbo(
    returns_matrix: np.ndarray,
    n_subsets: int = 16,
) -> PBOResult:
    """Compute PBO using the CSCV (Combinatorial Symmetric Cross-Validation) method.

    Args:
        returns_matrix: Matrix of shape (T, N) where T = time periods,
                       N = strategy configurations. Each column is a strategy's
                       return series.
        n_subsets: Number of subsets to partition the data into (must be even).
                  More subsets = more combinations but noisier per-subset estimates.

    Returns:
        PBOResult with probability of overfitting.
    """
    T, N = returns_matrix.shape

    if N < 2:
        return PBOResult(0.0, np.array([]), 0, N, n_subsets)

    if n_subsets % 2 != 0:
        n_subsets += 1  # must be even for symmetric split

    if T < n_subsets * 2:
        raise ValueError(
            f"Need at least {n_subsets * 2} observations, got {T}"
        )

    # Partition T periods into S subsets
    subset_size = T // n_subsets
    subsets = []
    for i in range(n_subsets):
        start = i * subset_size
        end = start + subset_size if i < n_subsets - 1 else T
        subsets.append((start, end))

    # Generate all C(S, S/2) combinations for in-sample half
    half = n_subsets // 2
    all_combos = list(combinations(range(n_subsets), half))

    # Limit combinations for computational feasibility
    max_combos = 500
    if len(all_combos) > max_combos:
        rng = np.random.RandomState(42)
        indices = rng.choice(len(all_combos), max_combos, replace=False)
        all_combos = [all_combos[i] for i in indices]

    logits = []

    for is_groups in all_combos:
        oos_groups = tuple(g for g in range(n_subsets) if g not in is_groups)

        # Build IS and OOS return matrices
        is_indices = np.concatenate([
            np.arange(subsets[g][0], subsets[g][1]) for g in is_groups
        ])
        oos_indices = np.concatenate([
            np.arange(subsets[g][0], subsets[g][1]) for g in oos_groups
        ])

        is_returns = returns_matrix[is_indices]   # (T/2, N)
        oos_returns = returns_matrix[oos_indices]  # (T/2, N)

        # Compute performance metric (Sharpe) for each strategy
        is_sharpes = _column_sharpes(is_returns)
        oos_sharpes = _column_sharpes(oos_returns)

        # Find IS-optimal strategy
        best_is_idx = np.argmax(is_sharpes)

        # Rank of IS-optimal strategy in OOS
        oos_rank = sp_stats.rankdata(oos_sharpes)[best_is_idx]
        oos_rank_pct = oos_rank / N  # normalized rank [0, 1]

        # Logit: log(p / (1-p)) where p = OOS rank percentile
        # If rank_pct == 1.0 or 0.0, clip to avoid inf
        p = np.clip(oos_rank_pct, 0.01, 0.99)
        logit_val = log(p / (1.0 - p))
        logits.append(logit_val)

    logits_arr = np.array(logits)

    # PBO = fraction of logits that are negative
    # (negative logit means IS-optimal strategy ranks below median OOS)
    pbo_val = float(np.mean(logits_arr < 0))

    return PBOResult(
        pbo=pbo_val,
        logit_distribution=logits_arr,
        n_combinations=len(logits_arr),
        n_strategies=N,
        n_subsets=n_subsets,
    )


# ===========================================================================
# Minimum Backtest Length
# ===========================================================================

@dataclass
class MinBTLResult:
    """Result of Minimum Backtest Length check."""
    min_years: float
    actual_years: float
    n_trials: int
    is_sufficient: bool

    def summary(self) -> str:
        status = "PASS" if self.is_sufficient else "FAIL"
        return (
            f"MinBTL: need {self.min_years:.1f}y, have {self.actual_years:.1f}y "
            f"(N={self.n_trials} trials) [{status}]"
        )


def min_btl(
    n_trials: int,
    actual_years: float,
    target_sharpe: float = 1.0,
) -> MinBTLResult:
    """Check if backtest length is sufficient given number of trials.

    Formula from Bailey et al. (2014) "Pseudo-Mathematics and Financial Charlatanism":
        MinBTL (years) ≈ (E[max_z(N)])^2 / target_sharpe^2

    Where E[max_z(N)] = expected max of N standard normals ≈ sqrt(2·ln(N)).
    The idea: with N trials, the best Sharpe inflates by E[max_z]/sqrt(T).
    To detect target_sharpe reliably, we need T large enough that this inflation
    is small relative to target_sharpe.

    Args:
        n_trials: Number of strategy configurations tested.
        actual_years: Actual backtest length in years.
        target_sharpe: Target annualized Sharpe ratio.
    """
    if n_trials < 1:
        n_trials = 1

    e_max_z = _expected_max_z(n_trials)
    min_years = max(1.0, (e_max_z / max(target_sharpe, 0.01)) ** 2)

    return MinBTLResult(
        min_years=min_years,
        actual_years=actual_years,
        n_trials=n_trials,
        is_sufficient=actual_years >= min_years,
    )


# ===========================================================================
# Helpers
# ===========================================================================

def _expected_max_z(n_trials: int) -> float:
    """Expected maximum of N i.i.d. standard normal draws.

    Uses the approximation:
        E[max] ≈ sqrt(2·ln(N)) - [ln(pi) + ln(ln(N))] / [2·sqrt(2·ln(N))]
    """
    if n_trials <= 1:
        return 0.0

    ln_n = log(max(n_trials, 2))

    if ln_n < 0.01:
        return 0.0

    sqrt_2ln = sqrt(2.0 * ln_n)
    correction = (log(pi) + log(max(ln_n, 0.01))) / (2.0 * sqrt_2ln)
    return max(0.0, sqrt_2ln - correction)


def _column_sharpes(returns: np.ndarray) -> np.ndarray:
    """Compute Sharpe for each column of a returns matrix."""
    means = returns.mean(axis=0)
    stds = returns.std(axis=0)
    stds = np.where(stds < 1e-12, 1e-12, stds)
    return means / stds
