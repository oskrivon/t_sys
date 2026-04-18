"""Signal detection — breakouts, retests, zakol (false breakout).

Extracted from scripts/research/miro_strategy_v3.py.
Pure functions, no I/O.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .models import Breakout, Level, Signal, SignalType


def detect_breakouts(
    df: pd.DataFrame,
    idx: int,
    levels: list[Level],
) -> list[Breakout]:
    """Detect new breakouts at candle `idx`.

    A breakout occurs when close crosses a level zone boundary.
    Returns list of new Breakout objects.
    """
    if idx < 1:
        return []

    close = df["close"].iloc[idx]
    prev_close = df["close"].iloc[idx - 1]
    breakouts = []

    for lv in levels:
        zh, zl = lv.zone_high, lv.zone_low
        # Breakout up
        if prev_close <= zh and close > zh * 1.001:
            breakouts.append(Breakout(level=lv, direction="long", idx=idx))
        # Breakout down
        if prev_close >= zl and close < zl * 0.999:
            breakouts.append(Breakout(level=lv, direction="short", idx=idx))

    return breakouts


def detect_retests(
    df: pd.DataFrame,
    idx: int,
    recent_breakouts: list[Breakout],
    retest_window: int = 15,
    trend: Optional[pd.Series] = None,
) -> Optional[tuple[SignalType, Level, int]]:
    """Detect retest signals from recent breakouts.

    Returns (signal_type, level, score) or None.
    """
    close = df["close"].iloc[idx]
    low = df["low"].iloc[idx]
    high = df["high"].iloc[idx]

    best: Optional[tuple[SignalType, Level, int]] = None
    best_score = 0

    for brk in recent_breakouts:
        age = idx - brk.idx
        if age < 1 or age > retest_window:
            continue

        lv = brk.level
        zh, zl = lv.zone_high, lv.zone_low

        # Long retest: price dips back to broken resistance (now support)
        if brk.direction == "long" and low <= zh * 1.005 and close > zh:
            if trend is not None and trend.iloc[idx] == -1:
                continue
            if lv.score > best_score:
                best = (SignalType.LONG_RETEST, lv, lv.score)
                best_score = lv.score

        # Short retest: price bounces back to broken support (now resistance)
        if brk.direction == "short" and high >= zl * 0.995 and close < zl:
            if trend is not None and trend.iloc[idx] == 1:
                continue
            if lv.score > best_score:
                best = (SignalType.SHORT_RETEST, lv, lv.score)
                best_score = lv.score

    return best


def detect_zakol(
    df: pd.DataFrame,
    idx: int,
    levels: list[Level],
    trend: Optional[pd.Series] = None,
) -> Optional[tuple[SignalType, Level, int]]:
    """Detect zakol (false breakout / liquidity grab) at candle `idx`.

    Zakol: wick pierces level zone, but body closes back inside.
    Returns (signal_type, level, score+1 bonus) or None.
    """
    close = df["close"].iloc[idx]
    low = df["low"].iloc[idx]
    high = df["high"].iloc[idx]

    best: Optional[tuple[SignalType, Level, int]] = None
    best_score = 0

    for lv in levels:
        zh, zl = lv.zone_high, lv.zone_low

        # Long zakol: wick below support, close above
        if low < zl * 0.995 and close > zl:
            if trend is not None and trend.iloc[idx] == -1:
                continue
            score = lv.score + 1  # zakol bonus
            if score > best_score:
                best = (SignalType.LONG_ZAKOL, lv, score)
                best_score = score

        # Short zakol: wick above resistance, close below
        if high > zh * 1.005 and close < zh:
            if trend is not None and trend.iloc[idx] == 1:
                continue
            score = lv.score + 1
            if score > best_score:
                best = (SignalType.SHORT_ZAKOL, lv, score)
                best_score = score

    return best


def find_best_signal(
    df: pd.DataFrame,
    idx: int,
    levels: list[Level],
    recent_breakouts: list[Breakout],
    retest_window: int = 15,
    trend: Optional[pd.Series] = None,
) -> Optional[tuple[SignalType, Level, int]]:
    """Find the best signal at candle `idx` across retests and zakol.

    Returns (signal_type, level, score) for the highest-scoring signal, or None.
    """
    retest = detect_retests(df, idx, recent_breakouts, retest_window, trend)
    zakol = detect_zakol(df, idx, levels, trend)

    if retest and zakol:
        return retest if retest[2] >= zakol[2] else zakol
    return retest or zakol


def build_signal(
    symbol: str,
    signal_type: SignalType,
    level: Level,
    entry_price: float,
    rr_ratio: float = 3.0,
    timestamp=None,
) -> Optional[Signal]:
    """Build a Signal with SL/TP from level zone.

    Returns None if SL distance is invalid.
    """
    zh, zl = level.zone_high, level.zone_low
    zone_w = zh - zl
    if zone_w < entry_price * 0.003:
        zone_w = entry_price * 0.005

    is_long = "long" in signal_type.value

    if is_long:
        sl = zl - zone_w * 0.3
        sl_dist = entry_price - sl
        tp = entry_price + sl_dist * rr_ratio
    else:
        sl = zh + zone_w * 0.3
        sl_dist = sl - entry_price
        tp = entry_price - sl_dist * rr_ratio

    # Validate SL distance
    if sl_dist <= 0 or sl_dist / entry_price > 0.08:
        return None

    return Signal(
        symbol=symbol,
        signal_type=signal_type,
        level=level,
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        is_long=is_long,
        level_score=level.score,
        timestamp=timestamp,
    )
