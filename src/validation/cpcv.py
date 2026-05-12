"""Combinatorial Purged Cross-Validation (CPCV).

Implements the method from Lopez de Prado (2018) "Advances in Financial ML".
Instead of a single walk-forward path, generates C(N,k) backtest paths,
each with purging (remove label overlap) and embargo (buffer after test boundary).

Usage:
    from src.validation.cpcv import cpcv_backtest
    result = cpcv_backtest(trades, train_fn, n_groups=10, n_test=2)
    print(f"Median Sharpe: {result.median_sharpe:.2f}")
    print(f"P(Sharpe > 0): {result.prob_positive:.0%}")
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import sqrt
from typing import Callable, Optional

import numpy as np
import pandas as pd

from src.backtest.models import Trade


@dataclass
class CPCVResult:
    """Result of Combinatorial Purged Cross-Validation."""
    sharpe_distribution: np.ndarray   # Sharpe for each path
    return_distribution: np.ndarray   # total return for each path
    n_paths: int
    n_groups: int
    n_test_groups: int
    purge_window: int
    embargo_window: int

    @property
    def median_sharpe(self) -> float:
        return float(np.median(self.sharpe_distribution))

    @property
    def mean_sharpe(self) -> float:
        return float(np.mean(self.sharpe_distribution))

    @property
    def std_sharpe(self) -> float:
        return float(np.std(self.sharpe_distribution))

    @property
    def sharpe_5th(self) -> float:
        return float(np.percentile(self.sharpe_distribution, 5))

    @property
    def prob_positive(self) -> float:
        return float(np.mean(self.sharpe_distribution > 0))

    def summary(self) -> str:
        return (
            f"CPCV: {self.n_paths} paths | "
            f"Sharpe median={self.median_sharpe:.2f} "
            f"mean={self.mean_sharpe:.2f} "
            f"std={self.std_sharpe:.2f} | "
            f"5th pct={self.sharpe_5th:.2f} | "
            f"P(>0)={self.prob_positive:.0%}"
        )


def cpcv_backtest(
    trades: list[Trade],
    n_groups: int = 10,
    n_test_groups: int = 2,
    purge_pct: float = 0.01,
    embargo_pct: float = 0.01,
    annualize_factor: float = 365.0,
) -> CPCVResult:
    """Run CPCV on a list of trades (no retraining — evaluate existing signals).

    This is the "signal evaluation" variant: we already have trades from a strategy.
    We split them into groups by time, hold out k groups as test, and compute
    test-set metrics across all C(N,k) combinations with purging + embargo.

    For ML strategies that need retraining, use cpcv_ml_backtest() instead.

    Args:
        trades: List of Trade objects sorted by exit_time.
        n_groups: Number of time groups to split trades into.
        n_test_groups: Number of groups to hold out as test in each combination.
        purge_pct: Fraction of trades to purge at train/test boundaries.
        embargo_pct: Fraction of trades to embargo after test end.
        annualize_factor: Trading days per year for Sharpe annualization.
    """
    if len(trades) < n_groups * 3:
        raise ValueError(
            f"Need at least {n_groups * 3} trades for {n_groups} groups, "
            f"got {len(trades)}"
        )

    trades_sorted = sorted(trades, key=lambda t: t.exit_time)
    n = len(trades_sorted)

    # Build daily returns series from trades
    returns_series = _trades_to_daily_returns(trades_sorted)
    n_days = len(returns_series)

    if n_days < n_groups:
        raise ValueError(
            f"Need at least {n_groups} days of data, got {n_days}"
        )

    # Split into N equal groups (by day index)
    group_size = n_days // n_groups
    group_indices = []
    for i in range(n_groups):
        start = i * group_size
        end = start + group_size if i < n_groups - 1 else n_days
        group_indices.append((start, end))

    purge_n = max(1, int(n_days * purge_pct))
    embargo_n = max(1, int(n_days * embargo_pct))

    # Generate all C(N, k) test combinations
    all_combos = list(combinations(range(n_groups), n_test_groups))
    sharpes = []
    total_returns = []

    for test_groups in all_combos:
        test_mask = np.zeros(n_days, dtype=bool)
        for g in test_groups:
            s, e = group_indices[g]
            test_mask[s:e] = True

        # Purge: remove observations near train/test boundaries
        purged_mask = test_mask.copy()
        for g in test_groups:
            s, e = group_indices[g]
            # Purge before test start (from train side)
            purge_start = max(0, s - purge_n)
            purged_mask[purge_start:s] = True
            # Embargo after test end
            embargo_end = min(n_days, e + embargo_n)
            purged_mask[e:embargo_end] = True

        # Test returns = only the test group days (not purged/embargoed)
        test_returns = returns_series.values[test_mask]

        if len(test_returns) < 5:
            continue

        # Compute Sharpe on test returns
        sharpe = _compute_sharpe(test_returns, annualize_factor)
        total_ret = float(np.sum(test_returns))

        sharpes.append(sharpe)
        total_returns.append(total_ret)

    sharpes_arr = np.array(sharpes)
    returns_arr = np.array(total_returns)

    return CPCVResult(
        sharpe_distribution=sharpes_arr,
        return_distribution=returns_arr,
        n_paths=len(sharpes_arr),
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        purge_window=purge_n,
        embargo_window=embargo_n,
    )


def cpcv_ml_backtest(
    features: pd.DataFrame,
    labels: pd.Series,
    train_predict_fn: Callable[[pd.DataFrame, pd.Series, pd.DataFrame], np.ndarray],
    returns: pd.Series,
    n_groups: int = 10,
    n_test_groups: int = 2,
    purge_pct: float = 0.02,
    embargo_pct: float = 0.01,
    annualize_factor: float = 365.0,
) -> CPCVResult:
    """CPCV for ML strategies that require retraining.

    Args:
        features: Feature matrix (DatetimeIndex, one row per observation).
        labels: Binary labels (1=win, 0=loss).
        train_predict_fn: Callable(X_train, y_train, X_test) -> predictions.
        returns: Per-observation returns (same index as features).
        n_groups: Number of time groups.
        n_test_groups: Number of test groups per combination.
        purge_pct: Purge fraction.
        embargo_pct: Embargo fraction.
    """
    n = len(features)
    if n < n_groups * 5:
        raise ValueError(f"Need at least {n_groups * 5} samples, got {n}")

    group_size = n // n_groups
    group_indices = []
    for i in range(n_groups):
        start = i * group_size
        end = start + group_size if i < n_groups - 1 else n
        group_indices.append((start, end))

    purge_n = max(1, int(n * purge_pct))
    embargo_n = max(1, int(n * embargo_pct))

    all_combos = list(combinations(range(n_groups), n_test_groups))
    sharpes = []
    total_returns = []

    for test_groups in all_combos:
        test_mask = np.zeros(n, dtype=bool)
        for g in test_groups:
            s, e = group_indices[g]
            test_mask[s:e] = True

        # Build purge/embargo exclusion zone
        exclude_mask = test_mask.copy()
        for g in test_groups:
            s, e = group_indices[g]
            purge_start = max(0, s - purge_n)
            exclude_mask[purge_start:s] = True
            embargo_end = min(n, e + embargo_n)
            exclude_mask[e:embargo_end] = True

        train_mask = ~exclude_mask

        if train_mask.sum() < 10 or test_mask.sum() < 5:
            continue

        X_train = features.iloc[train_mask.nonzero()[0]]
        y_train = labels.iloc[train_mask.nonzero()[0]]
        X_test = features.iloc[test_mask.nonzero()[0]]

        predictions = train_predict_fn(X_train, y_train, X_test)

        # Returns only for predicted-positive trades
        test_returns_all = returns.iloc[test_mask.nonzero()[0]].values
        if predictions.dtype == bool or set(np.unique(predictions)) <= {0, 1}:
            # Binary: only take trades where model says "go"
            traded_returns = test_returns_all[predictions.astype(bool)]
        else:
            # Continuous: weight returns by prediction
            traded_returns = test_returns_all * predictions

        if len(traded_returns) < 3:
            continue

        sharpe = _compute_sharpe(traded_returns, annualize_factor)
        total_ret = float(np.sum(traded_returns))

        sharpes.append(sharpe)
        total_returns.append(total_ret)

    return CPCVResult(
        sharpe_distribution=np.array(sharpes),
        return_distribution=np.array(total_returns),
        n_paths=len(sharpes),
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        purge_window=purge_n,
        embargo_window=embargo_n,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trades_to_daily_returns(
    trades: list[Trade],
    initial_capital: float = 10_000.0,
) -> pd.Series:
    """Convert list of trades to daily return series."""
    pnl_by_date: dict[pd.Timestamp, float] = {}
    for t in trades:
        day = pd.Timestamp(t.exit_time.date(), tz="UTC")
        pnl_by_date[day] = pnl_by_date.get(day, 0.0) + t.net_pnl_usd

    if not pnl_by_date:
        return pd.Series(dtype=float)

    # Fill full date range
    dates = pd.date_range(min(pnl_by_date), max(pnl_by_date), freq="D", tz="UTC")
    daily_pnl = pd.Series(0.0, index=dates)
    for dt, pnl in pnl_by_date.items():
        if dt in daily_pnl.index:
            daily_pnl[dt] = pnl

    # Convert to returns
    daily_returns = daily_pnl / initial_capital
    return daily_returns


def _compute_sharpe(
    returns: np.ndarray,
    annualize_factor: float = 365.0,
) -> float:
    """Annualized Sharpe ratio from a returns array."""
    if len(returns) < 2:
        return 0.0
    std = returns.std()
    if std < 1e-12:
        return 0.0
    return float(returns.mean() / std * sqrt(annualize_factor))
