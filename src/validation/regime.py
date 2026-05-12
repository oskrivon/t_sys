"""Regime detection and per-regime strategy analysis.

Implements HMM-based regime detection (2-3 states) and evaluates
strategy performance conditional on market regime.

Usage:
    from src.validation.regime import detect_regimes, regime_analysis

    regimes = detect_regimes(btc_returns)
    print(f"Current regime: {regimes.current_regime}")

    analysis = regime_analysis(strategy_returns, regimes)
    print(analysis.summary())
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Optional

import numpy as np
import pandas as pd


REGIME_NAMES = {
    0: "low_vol",     # calm / sideways
    1: "high_vol",    # volatile / trending
    2: "crisis",      # crash / extreme vol
}


@dataclass
class RegimeDetectionResult:
    """Result of regime detection on market data."""
    regime_labels: pd.Series        # regime label per day (DatetimeIndex)
    regime_probs: pd.DataFrame      # probability of each regime per day
    n_regimes: int
    regime_stats: dict[int, dict]   # {regime: {mean, std, n_days, pct}}
    transition_matrix: np.ndarray   # regime transition probabilities

    @property
    def current_regime(self) -> int:
        return int(self.regime_labels.iloc[-1])

    @property
    def current_regime_name(self) -> str:
        return REGIME_NAMES.get(self.current_regime, f"regime_{self.current_regime}")

    def summary(self) -> str:
        lines = [f"Regime Detection ({self.n_regimes} states):"]
        for r in range(self.n_regimes):
            s = self.regime_stats.get(r, {})
            name = REGIME_NAMES.get(r, f"regime_{r}")
            lines.append(
                f"  {name}: mean={s.get('mean', 0):.4f} std={s.get('std', 0):.4f} "
                f"days={s.get('n_days', 0)} ({s.get('pct', 0):.0%})"
            )
        lines.append(f"  Current: {self.current_regime_name}")
        return "\n".join(lines)


@dataclass
class RegimeAnalysisResult:
    """Per-regime strategy performance."""
    per_regime: dict[int, dict]     # {regime: {sharpe, return, n_trades, win_rate}}
    dominant_regime: int            # regime contributing most PnL
    dominant_pnl_pct: float         # fraction of total PnL from dominant regime
    is_multi_regime: bool           # profitable in 2+ regimes
    n_regimes_profitable: int

    def summary(self) -> str:
        lines = ["Regime Analysis:"]
        for r, stats in sorted(self.per_regime.items()):
            name = REGIME_NAMES.get(r, f"regime_{r}")
            sharpe = stats.get("sharpe", 0)
            ret = stats.get("total_return", 0)
            n = stats.get("n_observations", 0)
            status = "+" if ret > 0 else "-"
            lines.append(
                f"  {status} {name}: Sharpe={sharpe:.2f} "
                f"Return={ret:.2%} n_obs={n}"
            )

        dominant_name = REGIME_NAMES.get(self.dominant_regime, f"regime_{self.dominant_regime}")
        lines.append(
            f"  Dominant: {dominant_name} ({self.dominant_pnl_pct:.0%} of PnL)"
        )

        if self.is_multi_regime:
            lines.append(f"  Multi-regime: YES ({self.n_regimes_profitable} regimes profitable)")
        else:
            lines.append("  Multi-regime: NO -- concentrated in single regime [!]")

        return "\n".join(lines)


def detect_regimes(
    returns: pd.Series,
    n_regimes: int = 2,
    features: Optional[pd.DataFrame] = None,
) -> RegimeDetectionResult:
    """Detect market regimes using HMM or GMM fallback.

    Args:
        returns: Daily returns series (DatetimeIndex).
        n_regimes: Number of regimes to detect (2 or 3).
        features: Optional additional features (vol, correlation, etc.).
                 If None, uses returns + rolling vol as features.
    """
    if len(returns) < 60:
        raise ValueError(f"Need at least 60 observations, got {len(returns)}")

    # Build feature matrix
    if features is not None:
        X = features.reindex(returns.index).fillna(0).values
    else:
        vol_20 = returns.rolling(20).std().fillna(returns.std())
        X = np.column_stack([returns.values, vol_20.values])

    # Remove any NaN rows
    valid_mask = ~np.isnan(X).any(axis=1)
    X_clean = X[valid_mask]
    valid_index = returns.index[valid_mask]

    labels, probs, transmat = _fit_regime_model(X_clean, n_regimes)

    # Sort regimes by volatility (regime 0 = lowest vol)
    regime_vols = []
    for r in range(n_regimes):
        mask = labels == r
        if mask.sum() > 0:
            regime_vols.append(X_clean[mask, 0].std())
        else:
            regime_vols.append(0.0)

    sort_order = np.argsort(regime_vols)
    label_map = {old: new for new, old in enumerate(sort_order)}

    sorted_labels = np.array([label_map[l] for l in labels])
    sorted_probs = probs[:, sort_order]

    # Build result series
    regime_series = pd.Series(sorted_labels, index=valid_index, name="regime")
    prob_df = pd.DataFrame(
        sorted_probs,
        index=valid_index,
        columns=[REGIME_NAMES.get(i, f"regime_{i}") for i in range(n_regimes)],
    )

    # Regime statistics
    regime_stats = {}
    for r in range(n_regimes):
        mask = sorted_labels == r
        n_days = int(mask.sum())
        r_returns = X_clean[mask, 0] if mask.sum() > 0 else np.array([0.0])
        regime_stats[r] = {
            "mean": float(r_returns.mean()),
            "std": float(r_returns.std()),
            "n_days": n_days,
            "pct": n_days / len(sorted_labels) if len(sorted_labels) > 0 else 0,
        }

    # Reorder transition matrix
    sorted_transmat = transmat[sort_order][:, sort_order] if transmat is not None else np.eye(n_regimes)

    return RegimeDetectionResult(
        regime_labels=regime_series,
        regime_probs=prob_df,
        n_regimes=n_regimes,
        regime_stats=regime_stats,
        transition_matrix=sorted_transmat,
    )


def regime_analysis(
    strategy_returns: pd.Series,
    regimes: RegimeDetectionResult,
    annualize_factor: float = 365.0,
) -> RegimeAnalysisResult:
    """Analyze strategy performance per regime.

    Args:
        strategy_returns: Daily strategy returns (DatetimeIndex).
        regimes: Output from detect_regimes().
        annualize_factor: For Sharpe annualization.
    """
    common_idx = strategy_returns.index.intersection(regimes.regime_labels.index)
    if len(common_idx) < 10:
        return _empty_regime_analysis(regimes.n_regimes)

    ret = strategy_returns.loc[common_idx]
    labels = regimes.regime_labels.loc[common_idx]

    per_regime = {}
    total_pnl = float(ret.sum())

    for r in range(regimes.n_regimes):
        mask = labels == r
        r_ret = ret[mask]
        n_obs = len(r_ret)

        if n_obs < 2:
            per_regime[r] = {
                "sharpe": 0.0,
                "total_return": 0.0,
                "mean_return": 0.0,
                "n_observations": n_obs,
                "pnl_contribution": 0.0,
            }
            continue

        mean_r = float(r_ret.mean())
        std_r = float(r_ret.std())
        sharpe = mean_r / std_r * sqrt(annualize_factor) if std_r > 1e-12 else 0.0
        total_ret = float(r_ret.sum())

        per_regime[r] = {
            "sharpe": sharpe,
            "total_return": total_ret,
            "mean_return": mean_r,
            "n_observations": n_obs,
            "pnl_contribution": total_ret / total_pnl if abs(total_pnl) > 1e-12 else 0.0,
        }

    # Dominant regime
    pnl_by_regime = {r: s["total_return"] for r, s in per_regime.items()}
    dominant = max(pnl_by_regime, key=lambda r: abs(pnl_by_regime[r]))
    dominant_pct = abs(pnl_by_regime[dominant]) / abs(total_pnl) if abs(total_pnl) > 1e-12 else 1.0

    n_profitable = sum(1 for s in per_regime.values() if s["total_return"] > 0)
    is_multi = n_profitable >= 2

    return RegimeAnalysisResult(
        per_regime=per_regime,
        dominant_regime=dominant,
        dominant_pnl_pct=dominant_pct,
        is_multi_regime=is_multi,
        n_regimes_profitable=n_profitable,
    )


# ===========================================================================
# Model fitting
# ===========================================================================

def _fit_regime_model(
    X: np.ndarray,
    n_regimes: int,
) -> tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """Fit HMM or fallback to GMM for regime detection.

    Returns:
        labels: (n_samples,) array of regime labels
        probs: (n_samples, n_regimes) posterior probabilities
        transmat: (n_regimes, n_regimes) transition matrix or None
    """
    # Try HMM first
    try:
        from hmmlearn.hmm import GaussianHMM

        model = GaussianHMM(
            n_components=n_regimes,
            covariance_type="full",
            n_iter=200,
            random_state=42,
            tol=1e-4,
        )
        model.fit(X)
        labels = model.predict(X)
        probs = model.predict_proba(X)
        transmat = model.transmat_
        return labels, probs, transmat

    except ImportError:
        pass

    # Fallback: GMM (no temporal structure but still useful)
    try:
        from sklearn.mixture import GaussianMixture

        model = GaussianMixture(
            n_components=n_regimes,
            covariance_type="full",
            n_init=5,
            random_state=42,
        )
        model.fit(X)
        labels = model.predict(X)
        probs = model.predict_proba(X)

        # Estimate transition matrix from label sequence
        transmat = _estimate_transitions(labels, n_regimes)
        return labels, probs, transmat

    except ImportError:
        pass

    # Last resort: simple volatility threshold
    return _threshold_regimes(X, n_regimes)


def _estimate_transitions(labels: np.ndarray, n_states: int) -> np.ndarray:
    """Estimate transition matrix from a sequence of state labels."""
    trans = np.zeros((n_states, n_states))
    for i in range(len(labels) - 1):
        trans[labels[i], labels[i + 1]] += 1
    # Normalize rows
    row_sums = trans.sum(axis=1, keepdims=True)
    row_sums = np.where(row_sums == 0, 1, row_sums)
    return trans / row_sums


def _threshold_regimes(
    X: np.ndarray,
    n_regimes: int,
) -> tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """Simple volatility-threshold regime detection (no ML dependencies)."""
    # Use rolling volatility of first feature (returns)
    returns = X[:, 0]
    window = min(20, len(returns) // 3)
    vol = pd.Series(returns).rolling(window).std().fillna(returns.std()).values

    if n_regimes == 2:
        threshold = np.median(vol)
        labels = (vol > threshold).astype(int)
    else:
        q33, q66 = np.percentile(vol, [33, 66])
        labels = np.zeros(len(vol), dtype=int)
        labels[vol > q33] = 1
        labels[vol > q66] = 2

    # Build probability matrix (hard assignment)
    probs = np.zeros((len(labels), n_regimes))
    for i, l in enumerate(labels):
        probs[i, l] = 1.0

    transmat = _estimate_transitions(labels, n_regimes)
    return labels, probs, transmat


def _empty_regime_analysis(n_regimes: int) -> RegimeAnalysisResult:
    return RegimeAnalysisResult(
        per_regime={r: {"sharpe": 0, "total_return": 0, "mean_return": 0,
                        "n_observations": 0, "pnl_contribution": 0}
                    for r in range(n_regimes)},
        dominant_regime=0,
        dominant_pnl_pct=1.0,
        is_multi_regime=False,
        n_regimes_profitable=0,
    )
