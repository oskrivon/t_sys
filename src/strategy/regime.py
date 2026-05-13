"""Market regime detection — trending, ranging, or volatile.

Pure functions, no I/O.  Uses ADX, Kaufman efficiency ratio, and
volatility spike detection to classify the current market state.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


@dataclass(frozen=True, slots=True)
class RegimeState:
    """Result of regime detection at a given candle."""
    regime: Regime
    adx: float                 # 0-100, >25 = trending
    efficiency_ratio: float    # 0-1, ~1 = trend, ~0 = chop
    vol_ratio: float           # current_vol / avg_vol, >2 = spike
    trend_direction: int       # +1 long, -1 short, 0 neutral


def detect_regime(
    df: pd.DataFrame,
    idx: int,
    adx_period: int = 14,
    er_period: int = 20,
    vol_lookback: int = 60,
    adx_trend_threshold: float = 25.0,
    adx_range_threshold: float = 20.0,
    vol_spike_threshold: float = 2.0,
) -> RegimeState:
    """Classify market regime at candle ``idx``.

    Priority: VOLATILE > TRENDING > RANGING.

    Args:
        df: OHLCV DataFrame.
        idx: Current candle index.
        adx_period: Period for ADX calculation.
        er_period: Period for Kaufman efficiency ratio.
        vol_lookback: Lookback for average volatility baseline.
        adx_trend_threshold: ADX above this → trending.
        adx_range_threshold: ADX below this → ranging.
        vol_spike_threshold: vol_ratio above this → volatile.
    """
    # Need enough history
    min_history = max(adx_period * 2, vol_lookback, er_period) + 5
    if idx < min_history:
        return RegimeState(
            regime=Regime.RANGING,
            adx=0.0, efficiency_ratio=0.5, vol_ratio=1.0,
            trend_direction=0,
        )

    # -- ADX --
    adx_val, plus_di, minus_di = _compute_adx(df, idx, adx_period)

    # -- Kaufman Efficiency Ratio --
    er = _compute_efficiency_ratio(df, idx, er_period)

    # -- Volatility regime --
    vol_ratio = _compute_vol_ratio(df, idx, vol_lookback)

    # -- Trend direction --
    if plus_di > minus_di:
        trend_dir = 1
    elif minus_di > plus_di:
        trend_dir = -1
    else:
        trend_dir = 0

    # -- Classification (priority: volatile > trending > ranging) --
    if vol_ratio >= vol_spike_threshold:
        regime = Regime.VOLATILE
    elif adx_val >= adx_trend_threshold:
        regime = Regime.TRENDING
    else:
        regime = Regime.RANGING

    return RegimeState(
        regime=regime,
        adx=adx_val,
        efficiency_ratio=er,
        vol_ratio=vol_ratio,
        trend_direction=trend_dir,
    )


# ---------------------------------------------------------------------------
# Internal computations
# ---------------------------------------------------------------------------

def _compute_adx(
    df: pd.DataFrame, idx: int, period: int = 14,
) -> tuple[float, float, float]:
    """Compute ADX, +DI, -DI at index ``idx`` using Wilder smoothing.

    Full implementation: smooth TR/+DM/-DM → DI → DX series → smooth DX → ADX.
    """
    # Need 2*period bars minimum for ADX (period to seed + period to smooth DX)
    start = max(0, idx - period * 4)
    h = df["high"].iloc[start : idx + 1].values.astype(float)
    lo = df["low"].iloc[start : idx + 1].values.astype(float)
    c = df["close"].iloc[start : idx + 1].values.astype(float)

    n = len(h)
    if n < 2 * period + 1:
        return 0.0, 0.0, 0.0

    # True Range, +DM, -DM for each bar
    tr = np.empty(n - 1)
    plus_dm = np.empty(n - 1)
    minus_dm = np.empty(n - 1)

    for i in range(1, n):
        j = i - 1
        tr[j] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        up = h[i] - h[i - 1]
        down = lo[i - 1] - lo[i]
        plus_dm[j] = up if (up > down and up > 0) else 0.0
        minus_dm[j] = down if (down > up and down > 0) else 0.0

    # Wilder-smooth TR, +DM, -DM into full series
    atr_series = _wilder_smooth_series(tr, period)
    plus_series = _wilder_smooth_series(plus_dm, period)
    minus_series = _wilder_smooth_series(minus_dm, period)

    # Compute DI and DX series from smoothed values
    k = len(atr_series)
    dx_values = np.empty(k)
    last_plus_di = 0.0
    last_minus_di = 0.0

    for i in range(k):
        atr_val = atr_series[i]
        if atr_val <= 0:
            dx_values[i] = 0.0
            continue
        pdi = 100.0 * plus_series[i] / atr_val
        mdi = 100.0 * minus_series[i] / atr_val
        di_sum = pdi + mdi
        dx_values[i] = 100.0 * abs(pdi - mdi) / di_sum if di_sum > 0 else 0.0
        last_plus_di = pdi
        last_minus_di = mdi

    # ADX = Wilder-smooth of DX series
    if len(dx_values) >= period:
        adx = _wilder_smooth_scalar(dx_values, period)
    else:
        adx = float(dx_values[-1]) if len(dx_values) > 0 else 0.0

    return float(adx), float(last_plus_di), float(last_minus_di)


def _wilder_smooth_series(data: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing — return the full smoothed series (from period onward)."""
    if len(data) < period:
        return data.copy()

    result = np.empty(len(data) - period + 1)
    # Seed with SMA of first ``period`` values
    val = data[:period].sum()
    result[0] = val
    for i in range(period, len(data)):
        val = val - val / period + data[i]
        result[i - period + 1] = val
    return result


def _wilder_smooth_scalar(data: np.ndarray, period: int) -> float:
    """Wilder smoothing — return only the last smoothed value."""
    if len(data) < period:
        return float(data.mean()) if len(data) > 0 else 0.0

    val = data[:period].sum()
    for i in range(period, len(data)):
        val = val - val / period + data[i]
    return float(val / period)


def _compute_efficiency_ratio(
    df: pd.DataFrame, idx: int, period: int = 20,
) -> float:
    """Kaufman Efficiency Ratio: net_move / sum(abs_moves).

    Returns 0-1.  Near 1 = strong trend, near 0 = pure noise/chop.
    """
    if idx < period:
        return 0.5

    closes = df["close"].iloc[idx - period : idx + 1].values.astype(float)
    net_move = abs(closes[-1] - closes[0])
    abs_moves = np.sum(np.abs(np.diff(closes)))
    if abs_moves == 0:
        return 0.0
    return float(net_move / abs_moves)


def _compute_vol_ratio(
    df: pd.DataFrame, idx: int, lookback: int = 60,
) -> float:
    """Current realized vol vs longer-term average.

    Returns ratio > 1 if current vol is elevated.
    """
    if idx < lookback:
        return 1.0

    returns = df["close"].iloc[idx - lookback : idx + 1].pct_change().dropna().values
    if len(returns) < 20:
        return 1.0

    recent_vol = float(np.std(returns[-10:]))
    avg_vol = float(np.std(returns))
    if avg_vol <= 0:
        return 1.0
    return recent_vol / avg_vol
