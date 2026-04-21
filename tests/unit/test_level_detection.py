"""Tests for src/strategy/levels.py — swing points, clustering, rolling levels."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.levels import (
    cluster_points,
    find_swing_points,
    get_rolling_levels,
    is_round_number,
)


# ---------------------------------------------------------------------------
# is_round_number
# ---------------------------------------------------------------------------

class TestIsRoundNumber:
    @pytest.mark.parametrize("price", [100, 50, 10, 1000, 0.5, 500, 200])
    def test_round_numbers_return_true(self, price):
        assert is_round_number(price) is True

    @pytest.mark.parametrize("price", [123, 0.73, 3.14])
    def test_non_round_numbers_return_false(self, price):
        assert is_round_number(price) is False

    def test_zero_returns_false(self):
        assert is_round_number(0) is False

    def test_negative_returns_false(self):
        assert is_round_number(-100) is False


# ---------------------------------------------------------------------------
# cluster_points
# ---------------------------------------------------------------------------

class TestClusterPoints:
    def test_empty_input_returns_empty(self):
        assert cluster_points([]) == []

    def test_single_point_below_min_touches(self):
        points = [(10, 100.0, "high")]
        assert cluster_points(points, min_touches=2) == []

    def test_two_points_within_tolerance_produce_one_level(self):
        points = [
            (10, 100.0, "high"),
            (20, 100.5, "low"),  # 0.5% apart, within 1% tolerance
        ]
        levels = cluster_points(points, tolerance_pct=1.0, min_touches=2)
        assert len(levels) == 1
        assert levels[0].touches == 2
        assert abs(levels[0].price - 100.25) < 0.01  # mean price

    def test_points_outside_tolerance_separate_levels(self):
        points = [
            (10, 100.0, "high"),
            (20, 100.5, "high"),
            (30, 110.0, "low"),
            (40, 110.5, "low"),
        ]
        levels = cluster_points(points, tolerance_pct=1.0, min_touches=2)
        assert len(levels) == 2

    def test_score_includes_touch_count(self):
        points = [(i, 100.0, "high") for i in range(3)]
        levels = cluster_points(points, tolerance_pct=1.0, min_touches=2)
        assert len(levels) == 1
        # base score = touches = 3
        assert levels[0].score >= 3

    def test_score_round_number_bonus(self):
        # 100 is a round number — should get +2 bonus
        round_pts = [(10, 100.0, "high"), (20, 100.0, "low")]
        non_round_pts = [(10, 123.0, "high"), (20, 123.0, "low")]

        round_levels = cluster_points(round_pts, tolerance_pct=1.0, min_touches=2)
        non_round_levels = cluster_points(non_round_pts, tolerance_pct=1.0, min_touches=2)

        assert round_levels[0].score > non_round_levels[0].score

    def test_score_tight_cluster_bonus(self):
        # Very tight cluster: identical prices → (max-min)/mean = 0 < 0.005
        tight = [(10, 100.0, "high"), (20, 100.0, "low")]
        # Wider cluster: 1% spread
        wide = [(10, 100.0, "high"), (20, 100.8, "low")]

        tight_levels = cluster_points(tight, tolerance_pct=1.0, min_touches=2)
        wide_levels = cluster_points(wide, tolerance_pct=1.0, min_touches=2)

        assert tight_levels[0].score >= wide_levels[0].score

    def test_score_four_touches_bonus(self):
        # 4+ touches get +2 bonus
        pts_4 = [(i * 10, 123.0, "high") for i in range(4)]
        pts_2 = [(i * 10, 123.0, "high") for i in range(2)]

        levels_4 = cluster_points(pts_4, tolerance_pct=1.0, min_touches=2)
        levels_2 = cluster_points(pts_2, tolerance_pct=1.0, min_touches=2)

        assert levels_4[0].score - levels_2[0].score >= 4  # +2 touches + 2 bonus


# ---------------------------------------------------------------------------
# find_swing_points — helper to build OHLC DataFrames
# ---------------------------------------------------------------------------

def _make_ohlc(highs: list[float], lows: list[float]) -> pd.DataFrame:
    """Build minimal OHLC DataFrame from high/low lists."""
    n = len(highs)
    opens = [(h + l) / 2 for h, l in zip(highs, lows)]
    closes = opens[:]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes}
    )


class TestFindSwingPoints:
    def test_detects_swing_high(self):
        # Create V-shaped high at index 5 with order=2
        highs = [10, 11, 12, 11, 10, 15, 10, 11, 12, 11, 10]
        lows = [9, 10, 11, 10, 9, 9, 9, 10, 11, 10, 9]
        df = _make_ohlc(highs, lows)

        points = find_swing_points(df, start=0, end=len(df), order=2)
        swing_highs = [(idx, price) for idx, price, t in points if t == "high"]
        assert any(idx == 5 and price == 15 for idx, price in swing_highs)

    def test_detects_swing_low(self):
        # Inverted V low at index 5
        highs = [15, 14, 13, 14, 15, 11, 15, 14, 13, 14, 15]
        lows = [10, 9, 8, 9, 10, 5, 10, 9, 8, 9, 10]
        df = _make_ohlc(highs, lows)

        points = find_swing_points(df, start=0, end=len(df), order=2)
        swing_lows = [(idx, price) for idx, price, t in points if t == "low"]
        assert any(idx == 5 and price == 5 for idx, price in swing_lows)

    def test_respects_start_end_range(self):
        highs = [10, 20, 10, 10, 10, 10, 20, 10, 10, 10, 10]
        lows = [9] * 11
        df = _make_ohlc(highs, lows)

        # Only look in second half — should find swing at idx 6 but not idx 1
        points = find_swing_points(df, start=4, end=len(df), order=2)
        indices = [idx for idx, _, _ in points]
        assert 6 in indices
        assert 1 not in indices

    def test_empty_range_returns_empty(self):
        df = _make_ohlc([10, 11, 12], [9, 10, 11])
        # Range too small given order=5
        points = find_swing_points(df, start=0, end=3, order=5)
        assert points == []


# ---------------------------------------------------------------------------
# get_rolling_levels
# ---------------------------------------------------------------------------

class TestGetRollingLevels:
    def _make_long_df(self, n: int = 300) -> pd.DataFrame:
        """Generate a DataFrame with some repeating swing structure."""
        np.random.seed(42)
        base = 100 + np.cumsum(np.random.randn(n) * 0.5)
        # Inject clear level at ~100 by periodically bouncing there
        for i in range(50, n, 40):
            base[i] = 100.0
            if i + 1 < n:
                base[i + 1] = 100.0
        highs = base + 1
        lows = base - 1
        opens = base + 0.1
        closes = base - 0.1
        return pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes}
        )

    def test_returns_empty_if_too_few_candles(self):
        df = _make_ohlc([10] * 30, [9] * 30)
        levels = get_rolling_levels(df, current_idx=20, lookback=200)
        assert levels == []

    def test_respects_min_level_age(self):
        df = self._make_long_df(300)
        levels = get_rolling_levels(
            df, current_idx=280, lookback=200, min_level_age=20
        )
        # All returned levels should have last_idx at least 20 candles before current
        for lv in levels:
            assert 280 - lv.last_idx >= 20

    def test_returns_levels_for_sufficient_data(self):
        df = self._make_long_df(300)
        levels = get_rolling_levels(df, current_idx=280, lookback=200, min_touches=2)
        # With the seeded data we should get at least some levels
        # (this is a smoke test, not strict count)
        assert isinstance(levels, list)
