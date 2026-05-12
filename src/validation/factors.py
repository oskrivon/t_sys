"""Factor decomposition for crypto strategies.

Decomposes strategy returns into known crypto risk factors:
- CMKT: Crypto market factor (BTC excess return)
- CMOM: Crypto momentum (2-week, per Liu/Tsyvinski/Wu 2022)
- CSMB: Crypto size factor (small-cap vs large-cap)
- CARRY: Funding rate carry

The residual alpha (intercept) is the unexplained return — true alpha.

Usage:
    from src.validation.factors import factor_decomposition, build_crypto_factors
    factors = build_crypto_factors(btc_returns, alt_returns_dict, funding_rates)
    result = factor_decomposition(strategy_returns, factors)
    print(f"Alpha: {result.alpha_annual:.1%}  t-stat: {result.alpha_tstat:.2f}")
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class FactorResult:
    """Result of factor decomposition regression."""
    alpha: float                   # per-period intercept
    alpha_annual: float            # annualized alpha
    alpha_tstat: float             # t-statistic of alpha
    alpha_pvalue: float            # p-value of alpha
    betas: dict[str, float]        # factor loadings {name: beta}
    tstats: dict[str, float]       # t-stats per factor
    pvalues: dict[str, float]      # p-values per factor
    r_squared: float               # R² of the regression
    adj_r_squared: float           # adjusted R²
    n_observations: int
    factor_names: list[str]

    @property
    def has_alpha(self) -> bool:
        """True alpha exists: significant and positive."""
        return self.alpha_tstat > 2.0 and self.alpha > 0

    def summary(self) -> str:
        lines = [
            f"Factor Decomposition (n={self.n_observations}, R2={self.r_squared:.2f}):",
            f"  Alpha: {self.alpha_annual:.2%}/yr  (t={self.alpha_tstat:.2f}, p={self.alpha_pvalue:.3f})",
        ]
        for name in self.factor_names:
            b = self.betas[name]
            t = self.tstats[name]
            sig = "*" if abs(t) > 2.0 else ""
            lines.append(f"  {name:>8s}: b={b:+.4f}  (t={t:.2f}){sig}")
        return "\n".join(lines)


def factor_decomposition(
    strategy_returns: pd.Series,
    factor_returns: pd.DataFrame,
    annualize_factor: float = 365.0,
) -> FactorResult:
    """Regress strategy returns on factor returns.

    R_strategy = α + Σ βᵢ·Fᵢ + ε

    Args:
        strategy_returns: Daily strategy returns (DatetimeIndex).
        factor_returns: DataFrame with factor columns (DatetimeIndex).
                       Column names become factor names.
        annualize_factor: For annualizing alpha.
    """
    # Align indices
    common_idx = strategy_returns.index.intersection(factor_returns.index)
    if len(common_idx) < 10:
        return _empty_factor_result(factor_returns.columns.tolist())

    y = strategy_returns.loc[common_idx].values
    X = factor_returns.loc[common_idx].values
    factor_names = factor_returns.columns.tolist()
    n, k = X.shape

    # Add intercept
    X_with_const = np.column_stack([np.ones(n), X])

    # OLS: β = (X'X)^{-1} X'y
    try:
        XtX_inv = np.linalg.inv(X_with_const.T @ X_with_const)
    except np.linalg.LinAlgError:
        # Singular matrix — use pseudoinverse
        XtX_inv = np.linalg.pinv(X_with_const.T @ X_with_const)

    betas_all = XtX_inv @ (X_with_const.T @ y)

    alpha = float(betas_all[0])
    betas = {name: float(betas_all[i + 1]) for i, name in enumerate(factor_names)}

    # Residuals and standard errors
    y_hat = X_with_const @ betas_all
    residuals = y - y_hat
    sse = float(residuals @ residuals)
    dof = n - k - 1
    if dof < 1:
        dof = 1
    mse = sse / dof

    # Standard errors of coefficients
    se = np.sqrt(np.diag(XtX_inv) * mse)

    # t-statistics and p-values
    alpha_tstat = float(betas_all[0] / se[0]) if se[0] > 0 else 0.0
    alpha_pvalue = float(2.0 * (1.0 - _t_cdf(abs(alpha_tstat), dof)))

    tstats = {}
    pvalues = {}
    for i, name in enumerate(factor_names):
        t = float(betas_all[i + 1] / se[i + 1]) if se[i + 1] > 0 else 0.0
        tstats[name] = t
        pvalues[name] = float(2.0 * (1.0 - _t_cdf(abs(t), dof)))

    # R²
    ss_total = float(np.var(y) * n)
    r_squared = 1.0 - sse / ss_total if ss_total > 0 else 0.0
    adj_r_squared = 1.0 - (1.0 - r_squared) * (n - 1) / dof

    alpha_annual = alpha * annualize_factor

    return FactorResult(
        alpha=alpha,
        alpha_annual=alpha_annual,
        alpha_tstat=alpha_tstat,
        alpha_pvalue=alpha_pvalue,
        betas=betas,
        tstats=tstats,
        pvalues=pvalues,
        r_squared=r_squared,
        adj_r_squared=adj_r_squared,
        n_observations=n,
        factor_names=factor_names,
    )


# ===========================================================================
# Factor construction helpers
# ===========================================================================

def build_crypto_factors(
    btc_returns: pd.Series,
    alt_returns: Optional[dict[str, pd.Series]] = None,
    funding_rates: Optional[pd.Series] = None,
    momentum_window: int = 14,
) -> pd.DataFrame:
    """Build standard crypto factor returns from raw data.

    Args:
        btc_returns: Daily BTC returns (DatetimeIndex).
        alt_returns: Dict of {symbol: daily_returns} for alts.
                    Used to construct CSMB and CMOM factors.
        funding_rates: Daily average funding rate across major coins.
                      Used as the CARRY factor.
        momentum_window: Lookback for momentum factor (default 14 days = 2 weeks).
    """
    factors = pd.DataFrame(index=btc_returns.index)

    # CMKT: BTC excess return (risk-free ≈ 0 for crypto)
    factors["CMKT"] = btc_returns

    if alt_returns and len(alt_returns) >= 4:
        # Build equal-weight alt index
        alt_df = pd.DataFrame(alt_returns)
        common_idx = alt_df.dropna().index.intersection(btc_returns.index)
        alt_df = alt_df.loc[common_idx]

        # CMOM: 2-week momentum factor (long winners, short losers)
        # Rolling 14-day return, then long top quartile - short bottom quartile
        rolling_ret = alt_df.rolling(momentum_window).sum()
        n_coins = alt_df.shape[1]
        q_size = max(1, n_coins // 4)

        cmom = pd.Series(0.0, index=common_idx)
        for i, dt in enumerate(common_idx):
            if i < momentum_window:
                continue
            ranks = rolling_ret.loc[dt].rank()
            if ranks.isna().all():
                continue
            winners = ranks.nlargest(q_size).index
            losers = ranks.nsmallest(q_size).index
            long_ret = alt_df.loc[dt, winners].mean()
            short_ret = alt_df.loc[dt, losers].mean()
            cmom.loc[dt] = long_ret - short_ret

        factors["CMOM"] = cmom.reindex(factors.index, fill_value=0)

        # CSMB: size factor (not directly available without mcap data)
        # Proxy: use volatility as inverse size proxy (small caps = higher vol)
        vol_20d = alt_df.rolling(20).std()
        cmb = pd.Series(0.0, index=common_idx)
        for i, dt in enumerate(common_idx):
            if i < 20:
                continue
            vol_ranks = vol_20d.loc[dt].rank()
            if vol_ranks.isna().all():
                continue
            high_vol = vol_ranks.nlargest(q_size).index   # "small" proxy
            low_vol = vol_ranks.nsmallest(q_size).index    # "large" proxy
            cmb.loc[dt] = alt_df.loc[dt, high_vol].mean() - alt_df.loc[dt, low_vol].mean()

        factors["CSMB"] = cmb.reindex(factors.index, fill_value=0)

    if funding_rates is not None:
        factors["CARRY"] = funding_rates.reindex(factors.index, fill_value=0)

    return factors.fillna(0)


def build_factors_from_candles(
    candles: dict[str, pd.DataFrame],
    funding: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Build factors from raw OHLCV candle DataFrames.

    Args:
        candles: Dict of {symbol: DataFrame} with columns [open, high, low, close, volume].
                Must include 'BTC/USDT' or 'BTCUSDT'.
        funding: Optional daily funding rate series.
    """
    # Find BTC
    btc_key = None
    for key in candles:
        if "BTC" in key.upper():
            btc_key = key
            break

    if btc_key is None:
        raise ValueError("BTC candles required for factor construction")

    btc_df = candles[btc_key]
    btc_returns = btc_df["close"].pct_change().dropna()

    alt_returns = {}
    for symbol, df in candles.items():
        if symbol == btc_key:
            continue
        ret = df["close"].pct_change().dropna()
        if len(ret) > 30:
            alt_returns[symbol] = ret

    return build_crypto_factors(
        btc_returns=btc_returns,
        alt_returns=alt_returns if len(alt_returns) >= 4 else None,
        funding_rates=funding,
    )


# ===========================================================================
# Helpers
# ===========================================================================

def _t_cdf(t: float, dof: int) -> float:
    """CDF of Student's t-distribution (without scipy dependency at call time)."""
    try:
        from scipy.stats import t as t_dist
        return float(t_dist.cdf(t, dof))
    except ImportError:
        # Rough normal approximation for large dof
        from math import erf
        return 0.5 * (1.0 + erf(t / sqrt(2.0)))


def _empty_factor_result(factor_names: list[str]) -> FactorResult:
    return FactorResult(
        alpha=0.0,
        alpha_annual=0.0,
        alpha_tstat=0.0,
        alpha_pvalue=1.0,
        betas={n: 0.0 for n in factor_names},
        tstats={n: 0.0 for n in factor_names},
        pvalues={n: 1.0 for n in factor_names},
        r_squared=0.0,
        adj_r_squared=0.0,
        n_observations=0,
        factor_names=factor_names,
    )
