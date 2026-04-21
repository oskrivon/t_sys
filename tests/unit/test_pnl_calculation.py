"""Tests for PnL calculation and TP/SL detection in paper trading.

Validates:
- PnL formula: long = (close - entry) / entry * 100
                short = (entry - close) / entry * 100
- TP/SL detection: SL checked first (priority on same candle)
- Long: low <= SL triggers stop, high >= TP triggers take-profit
- Short: high >= SL triggers stop, low <= TP triggers take-profit
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Pure logic extracted for testing (mirrors service.py lines 171-185)
# ---------------------------------------------------------------------------

def compute_pnl_pct(entry: float, close_price: float, is_long: bool) -> float:
    """Compute PnL percentage. Mirrors paper_trading/service.py line 185."""
    if entry == 0:
        return 0.0
    if is_long:
        return (close_price - entry) / entry * 100
    else:
        return (entry - close_price) / entry * 100


def detect_tp_sl(
    is_long: bool,
    high: float,
    low: float,
    tp: float,
    sl: float,
) -> tuple[str | None, float | None]:
    """Detect TP/SL hit. Returns (status, close_price) or (None, None).

    Mirrors paper_trading/service.py lines 171-180.
    SL is checked first, so if both TP and SL are hit on the same candle,
    SL takes priority.
    """
    if is_long:
        if low <= sl:
            return "sl_hit", sl
        elif high >= tp:
            return "tp_hit", tp
    else:
        if high >= sl:
            return "sl_hit", sl
        elif low <= tp:
            return "tp_hit", tp
    return None, None


# ---------------------------------------------------------------------------
# PnL calculation tests
# ---------------------------------------------------------------------------

class TestPnlCalculation:
    """Test PnL percentage formula for long and short positions."""

    @pytest.mark.parametrize("entry,close,is_long,expected", [
        (100, 110, True, 10.0),    # Long profit
        (100, 90, True, -10.0),    # Long loss
        (100, 90, False, 10.0),    # Short profit
        (100, 110, False, -10.0),  # Short loss
        (100, 100, True, 0.0),     # Long break-even
        (100, 100, False, 0.0),    # Short break-even
        (50, 75, True, 50.0),      # Long +50%
        (50, 25, False, 50.0),     # Short +50%
        (200, 100, True, -50.0),   # Long -50%
        (200, 300, False, -50.0),  # Short -50%
    ])
    def test_pnl_parametrized(self, entry, close, is_long, expected):
        """Parametrized PnL calculation for common scenarios."""
        result = compute_pnl_pct(entry, close, is_long)
        assert result == pytest.approx(expected)

    def test_long_entry_100_close_110(self):
        """Long: entry=100, close=110 -> +10%."""
        assert compute_pnl_pct(100, 110, is_long=True) == pytest.approx(10.0)

    def test_long_entry_100_close_90(self):
        """Long: entry=100, close=90 -> -10%."""
        assert compute_pnl_pct(100, 90, is_long=True) == pytest.approx(-10.0)

    def test_short_entry_100_close_90(self):
        """Short: entry=100, close=90 -> +10% (profit on down move)."""
        assert compute_pnl_pct(100, 90, is_long=False) == pytest.approx(10.0)

    def test_short_entry_100_close_110(self):
        """Short: entry=100, close=110 -> -10% (loss on up move)."""
        assert compute_pnl_pct(100, 110, is_long=False) == pytest.approx(-10.0)

    def test_entry_zero_returns_zero(self):
        """Edge case: entry=0 should not raise ZeroDivisionError."""
        result = compute_pnl_pct(0, 100, is_long=True)
        assert result == 0.0

    def test_small_price_movement(self):
        """Tiny price movement: entry=50000, close=50001 -> +0.002%."""
        result = compute_pnl_pct(50000, 50001, is_long=True)
        assert result == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# TP/SL detection tests
# ---------------------------------------------------------------------------

class TestTpSlDetection:
    """Test TP/SL hit detection logic."""

    # -- Long positions --

    def test_long_sl_hit(self):
        """Long: candle low touches SL -> sl_hit."""
        status, price = detect_tp_sl(
            is_long=True, high=105, low=95, tp=110, sl=95,
        )
        assert status == "sl_hit"
        assert price == 95

    def test_long_tp_hit(self):
        """Long: candle high touches TP -> tp_hit."""
        status, price = detect_tp_sl(
            is_long=True, high=115, low=101, tp=110, sl=90,
        )
        assert status == "tp_hit"
        assert price == 110

    def test_long_neither(self):
        """Long: price stays between SL and TP -> no hit."""
        status, price = detect_tp_sl(
            is_long=True, high=105, low=95, tp=110, sl=90,
        )
        assert status is None
        assert price is None

    def test_long_exact_sl_match(self):
        """Long: low == SL exactly -> sl_hit (uses <=)."""
        status, price = detect_tp_sl(
            is_long=True, high=105, low=90, tp=110, sl=90,
        )
        assert status == "sl_hit"
        assert price == 90

    def test_long_exact_tp_match(self):
        """Long: high == TP exactly -> tp_hit (uses >=)."""
        status, price = detect_tp_sl(
            is_long=True, high=110, low=95, tp=110, sl=90,
        )
        assert status == "tp_hit"
        assert price == 110

    # -- Short positions --

    def test_short_sl_hit(self):
        """Short: candle high touches SL -> sl_hit."""
        status, price = detect_tp_sl(
            is_long=False, high=105, low=88, tp=85, sl=105,
        )
        assert status == "sl_hit"
        assert price == 105

    def test_short_tp_hit(self):
        """Short: candle low touches TP -> tp_hit."""
        status, price = detect_tp_sl(
            is_long=False, high=99, low=84, tp=85, sl=110,
        )
        assert status == "tp_hit"
        assert price == 85

    def test_short_neither(self):
        """Short: price stays between TP and SL -> no hit."""
        status, price = detect_tp_sl(
            is_long=False, high=99, low=91, tp=85, sl=110,
        )
        assert status is None
        assert price is None

    def test_short_exact_sl_match(self):
        """Short: high == SL exactly -> sl_hit (uses >=)."""
        status, price = detect_tp_sl(
            is_long=False, high=110, low=95, tp=85, sl=110,
        )
        assert status == "sl_hit"
        assert price == 110

    def test_short_exact_tp_match(self):
        """Short: low == TP exactly -> tp_hit (uses <=)."""
        status, price = detect_tp_sl(
            is_long=False, high=99, low=85, tp=85, sl=110,
        )
        assert status == "tp_hit"
        assert price == 85

    # -- Same-candle priority --

    def test_long_both_hit_sl_priority(self):
        """Long: both SL and TP touched same candle -> SL wins (checked first)."""
        status, price = detect_tp_sl(
            is_long=True, high=115, low=85, tp=110, sl=90,
        )
        assert status == "sl_hit"
        assert price == 90

    def test_short_both_hit_sl_priority(self):
        """Short: both SL and TP touched same candle -> SL wins (checked first)."""
        status, price = detect_tp_sl(
            is_long=False, high=115, low=80, tp=85, sl=110,
        )
        assert status == "sl_hit"
        assert price == 110

    # -- Parametrized edge cases --

    @pytest.mark.parametrize("is_long,high,low,tp,sl,exp_status,exp_price", [
        # Long: SL barely touched
        (True, 100, 89.99, 110, 90, "sl_hit", 90),
        # Long: TP barely touched
        (True, 110.01, 95, 110, 90, "tp_hit", 110),
        # Short: SL barely touched
        (False, 110.01, 95, 85, 110, "sl_hit", 110),
        # Short: TP barely touched
        (False, 99, 84.99, 85, 110, "tp_hit", 85),
        # Long: wide candle, low well below SL
        (True, 150, 50, 120, 80, "sl_hit", 80),
        # Short: wide candle, high well above SL
        (False, 150, 50, 70, 120, "sl_hit", 120),
    ])
    def test_edge_cases_parametrized(self, is_long, high, low, tp, sl,
                                     exp_status, exp_price):
        """Parametrized edge cases for TP/SL detection."""
        status, price = detect_tp_sl(is_long=is_long, high=high, low=low,
                                     tp=tp, sl=sl)
        assert status == exp_status
        assert price == exp_price
