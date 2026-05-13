"""Feature computation for ML classifier.

Extracted from scripts/research/miro_ml_classifier.py.
32 features used by GradientBoosting to predict P(win).
(29 base + 3 regime features added in v2.)
"""
from __future__ import annotations

import pandas as pd

from .levels import is_round_number
from .models import Level, SignalType
from .regime import detect_regime


def compute_features(
    df: pd.DataFrame,
    idx: int,
    level: Level,
    signal_type: SignalType,
    trend_sma: int = 50,
) -> dict[str, float]:
    """Compute 29 features for a signal at candle `idx`.

    Args:
        df: OHLCV DataFrame with columns [ts, open, high, low, close, volume].
        idx: Index of the signal candle.
        level: The S/R level that triggered the signal.
        signal_type: Type of signal detected.
        trend_sma: SMA period for trend features.

    Returns:
        Dict of feature_name -> value.
    """
    close = df["close"].iloc[idx]
    high = df["high"].iloc[idx]
    low = df["low"].iloc[idx]
    open_ = df["open"].iloc[idx]
    volume = df["volume"].iloc[idx]

    f: dict[str, float] = {}

    # -- Level features --
    f["level_touches"] = level.touches
    f["level_score"] = level.score
    f["level_age"] = idx - level.last_idx
    f["is_round_number"] = 1.0 if is_round_number(level.price) else 0.0

    zone_w = level.zone_width
    if zone_w <= 0:
        zone_w = close * 0.005
    f["zone_width_pct"] = zone_w / close * 100

    f["distance_to_level_pct"] = abs(close - level.price) / close * 100

    # -- Signal type --
    f["is_retest"] = 1.0 if "retest" in signal_type.value else 0.0
    f["is_zakol"] = 1.0 if "zakol" in signal_type.value else 0.0
    f["is_long"] = 1.0 if "long" in signal_type.value else 0.0

    # -- Candle features --
    candle_range = high - low
    if candle_range > 0:
        f["body_ratio"] = abs(close - open_) / candle_range
        f["upper_wick_ratio"] = (high - max(close, open_)) / candle_range
        f["lower_wick_ratio"] = (min(close, open_) - low) / candle_range
    else:
        f["body_ratio"] = 0.0
        f["upper_wick_ratio"] = 0.0
        f["lower_wick_ratio"] = 0.0

    f["is_green"] = 1.0 if close > open_ else 0.0

    # -- Trend features --
    if idx >= trend_sma:
        sma_50 = df["close"].iloc[max(0, idx - trend_sma) : idx].mean()
    else:
        sma_50 = close
    f["distance_to_sma50_pct"] = (close - sma_50) / sma_50 * 100

    sma_20 = df["close"].iloc[max(0, idx - 20) : idx].mean() if idx >= 20 else close
    f["distance_to_sma20_pct"] = (close - sma_20) / sma_20 * 100

    if idx >= 10:
        recent = df.iloc[idx - 10 : idx]
        f["green_candles_10"] = float((recent["close"] > recent["open"]).sum())
        f["trend_strength_10"] = (
            (df["close"].iloc[idx] - df["close"].iloc[idx - 10])
            / df["close"].iloc[idx - 10]
            * 100
        )
    else:
        f["green_candles_10"] = 5.0
        f["trend_strength_10"] = 0.0

    # -- Volatility features --
    if idx >= 20:
        returns = df["close"].iloc[idx - 20 : idx].pct_change().dropna()
        f["volatility_20"] = float(returns.std() * 100)
        f["atr_20"] = float(
            (df["high"].iloc[idx - 20 : idx] - df["low"].iloc[idx - 20 : idx]).mean()
            / close
            * 100
        )
    else:
        f["volatility_20"] = 1.0
        f["atr_20"] = 1.0

    f["atr_vs_zone"] = f["atr_20"] / f["zone_width_pct"] if f["zone_width_pct"] > 0 else 1.0

    # -- Volume features --
    if idx >= 30:
        avg_vol_30 = df["volume"].iloc[idx - 30 : idx].mean()
        f["volume_ratio"] = float(volume / avg_vol_30) if avg_vol_30 > 0 else 1.0
        vol_recent = df["volume"].iloc[idx - 5 : idx].mean()
        vol_older = df["volume"].iloc[idx - 30 : idx - 5].mean()
        f["volume_trend"] = float(vol_recent / vol_older) if vol_older > 0 else 1.0
    else:
        f["volume_ratio"] = 1.0
        f["volume_trend"] = 1.0

    # -- "Coin in play" proxy --
    if idx >= 180:
        f["price_change_7d"] = (close / df["close"].iloc[idx - 42] - 1) * 100
        f["price_change_30d"] = (close / df["close"].iloc[idx - 180] - 1) * 100
        f["abs_move_7d"] = abs(f["price_change_7d"])
        f["abs_move_30d"] = abs(f["price_change_30d"])
    elif idx >= 42:
        f["price_change_7d"] = (close / df["close"].iloc[idx - 42] - 1) * 100
        f["price_change_30d"] = 0.0
        f["abs_move_7d"] = abs(f["price_change_7d"])
        f["abs_move_30d"] = 0.0
    else:
        f["price_change_7d"] = 0.0
        f["price_change_30d"] = 0.0
        f["abs_move_7d"] = 0.0
        f["abs_move_30d"] = 0.0

    # -- RSI --
    if idx >= 14:
        deltas = df["close"].iloc[idx - 14 : idx + 1].diff().dropna()
        gains = deltas.clip(lower=0).mean()
        losses = (-deltas.clip(upper=0)).mean()
        rs = gains / losses if losses > 0 else 100
        f["rsi_14"] = 100 - (100 / (1 + rs))
    else:
        f["rsi_14"] = 50.0

    # -- Time features --
    ts = df["ts"].iloc[idx]
    if hasattr(ts, "hour"):
        f["hour_utc"] = float(ts.hour)
        f["day_of_week"] = float(ts.dayofweek)
    else:
        f["hour_utc"] = 12.0
        f["day_of_week"] = 3.0

    # -- Regime features --
    regime_state = detect_regime(df, idx)
    f["regime_adx"] = regime_state.adx
    f["regime_efficiency"] = regime_state.efficiency_ratio
    f["regime_vol_ratio"] = regime_state.vol_ratio

    return f
