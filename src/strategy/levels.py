"""Level detection — swing points, clustering, rolling levels.

Extracted from scripts/research/miro_strategy_v3.py.
Pure functions, no I/O.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .models import Level


def is_round_number(price: float) -> bool:
    """Check if price is at a psychologically significant round number."""
    if price <= 0:
        return False
    magnitude = 10 ** int(math.log10(abs(price)))
    for div in [magnitude, magnitude / 2, magnitude / 5, magnitude / 10]:
        if div > 0:
            rem = price % div
            if rem / div < 0.02 or rem / div > 0.98:
                return True
    return False


def find_swing_points(
    df: pd.DataFrame,
    start: int,
    end: int,
    order: int = 5,
    causal: bool = True,
) -> list[tuple[int, float, str]]:
    """Find swing highs/lows in range [start, end).

    Args:
        causal: If True (default), only uses data up to ``end`` — no future
            candles beyond the boundary are read.  Safe for live trading and
            backtests.  When False, the classic symmetric window is used
            (requires ``order`` candles after each candidate).

    Returns list of (idx, price, "high"|"low").
    """
    points = []
    if causal:
        # Causal mode: the window for candidate *i* is [i-order, i].
        # We need at least ``order`` candles before the candidate.
        lo = max(start + order, order)
        hi = min(end, len(df))
        for i in range(lo, hi):
            window_high = df["high"].iloc[i - order : i + 1]
            if df["high"].iloc[i] == window_high.max():
                points.append((i, df["high"].iloc[i], "high"))
            window_low = df["low"].iloc[i - order : i + 1]
            if df["low"].iloc[i] == window_low.min():
                points.append((i, df["low"].iloc[i], "low"))
    else:
        # Non-causal: symmetric window [i-order, i+order].
        # Useful for offline analysis on complete datasets.
        for i in range(max(start, order), min(end, len(df) - order)):
            window_high = df["high"].iloc[i - order : i + order + 1]
            if df["high"].iloc[i] == window_high.max():
                points.append((i, df["high"].iloc[i], "high"))
            window_low = df["low"].iloc[i - order : i + order + 1]
            if df["low"].iloc[i] == window_low.min():
                points.append((i, df["low"].iloc[i], "low"))
    return points


def cluster_points(
    points: list[tuple[int, float, str]],
    tolerance_pct: float = 1.0,
    min_touches: int = 2,
) -> list[Level]:
    """Cluster swing points into S/R levels.

    Args:
        points: List of (idx, price, type) from find_swing_points.
        tolerance_pct: Max distance (%) to group points into same level.
        min_touches: Minimum swing points required per level.

    Returns:
        List of Level objects sorted by score descending.
    """
    if not points:
        return []

    sorted_pts = sorted(points, key=lambda p: p[1])
    levels = []
    used: set[int] = set()

    for i, (idx, price, _) in enumerate(sorted_pts):
        if i in used:
            continue
        cluster = [(idx, price)]
        used.add(i)
        for j in range(i + 1, len(sorted_pts)):
            if j in used:
                continue
            if abs(sorted_pts[j][1] - price) / price < tolerance_pct / 100:
                cluster.append((sorted_pts[j][0], sorted_pts[j][1]))
                used.add(j)

        if len(cluster) >= min_touches:
            prices = [p[1] for p in cluster]
            indices = [p[0] for p in cluster]
            mean_p = float(np.mean(prices))

            score = len(cluster)
            if is_round_number(mean_p):
                score += 2
            if (max(prices) - min(prices)) / mean_p < 0.005:
                score += 1
            if len(cluster) >= 4:
                score += 2

            levels.append(Level(
                price=mean_p,
                touches=len(cluster),
                zone_high=max(prices),
                zone_low=min(prices),
                first_idx=min(indices),
                last_idx=max(indices),
                score=score,
            ))

    return sorted(levels, key=lambda x: x.score, reverse=True)


def get_rolling_levels(
    df: pd.DataFrame,
    current_idx: int,
    lookback: int = 200,
    min_touches: int = 2,
    tolerance_pct: float = 1.0,
    min_level_age: int = 20,
    swing_order: int = 5,
) -> list[Level]:
    """Get S/R levels from the last `lookback` candles.

    Only returns levels whose last touch is at least `min_level_age`
    candles ago (no look-ahead bias).  Uses causal swing detection —
    no future data beyond ``current_idx`` is ever read.
    """
    start = max(0, current_idx - lookback)
    end = current_idx  # causal mode handles the boundary internally

    if end - start < 50:
        return []

    points = find_swing_points(df, start, end, order=swing_order, causal=True)
    levels = cluster_points(points, tolerance_pct, min_touches)

    return [lv for lv in levels if current_idx - lv.last_idx >= min_level_age]
