"""Tests for TP/SL detection logic from PaperTradingService._check_trade.

The logic is extracted into a pure function for testability. This avoids
needing Redis/SQLite connections that the full service requires.

Rules:
- Long: SL hit when low <= sl, TP hit when high >= tp. SL checked first.
- Short: SL hit when high >= sl, TP hit when low <= tp. SL checked first.
- PnL: long = (close - entry) / entry * 100
         short = (entry - close) / entry * 100
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Extracted logic (mirrors PaperTradingService._check_trade lines 158-194)
# ---------------------------------------------------------------------------

def check_tp_sl(
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    high: float,
    low: float,
    last: float,
) -> dict | None:
    """Pure-function replica of _check_trade TP/SL detection.

    Returns None if neither TP nor SL was hit, otherwise a dict with
    status, close_price, and pnl_pct.
    """
    is_long = direction == "long"
    status = None
    close_price = last

    if is_long:
        if low <= sl:
            status, close_price = "sl_hit", sl
        elif high >= tp:
            status, close_price = "tp_hit", tp
    else:
        if high >= sl:
            status, close_price = "sl_hit", sl
        elif low <= tp:
            status, close_price = "tp_hit", tp

    if not status:
        return None

    pnl_pct = (
        ((close_price - entry) / entry * 100)
        if is_long
        else ((entry - close_price) / entry * 100)
    )
    return {"status": status, "close_price": close_price, "pnl_pct": pnl_pct}


# ---------------------------------------------------------------------------
# Long position — SL tests
# ---------------------------------------------------------------------------

class TestLongSL:
    """Stop-loss detection for long positions."""

    @pytest.mark.parametrize(
        "low, sl, expected_pnl",
        [
            (95.0, 95.0, -5.0),      # exact touch
            (90.0, 95.0, -5.0),      # gap through SL
            (94.99, 95.0, -5.0),     # low below SL
        ],
        ids=["exact-touch", "gap-through", "below-sl"],
    )
    def test_sl_hit(self, low, sl, expected_pnl):
        result = check_tp_sl("long", entry=100, sl=sl, tp=130, high=105, low=low, last=96)
        assert result is not None
        assert result["status"] == "sl_hit"
        assert result["close_price"] == sl
        assert result["pnl_pct"] == pytest.approx(expected_pnl)

    def test_price_just_above_sl_no_hit(self):
        """Low is 95.01, SL is 95 → no hit (95.01 > 95)."""
        result = check_tp_sl("long", entry=100, sl=95, tp=130, high=110, low=95.01, last=100)
        assert result is None


# ---------------------------------------------------------------------------
# Long position — TP tests
# ---------------------------------------------------------------------------

class TestLongTP:
    """Take-profit detection for long positions."""

    @pytest.mark.parametrize(
        "high, tp, expected_pnl",
        [
            (130.0, 130.0, 30.0),    # exact touch
            (135.0, 130.0, 30.0),    # gap through TP
        ],
        ids=["exact-touch", "gap-through"],
    )
    def test_tp_hit(self, high, tp, expected_pnl):
        result = check_tp_sl("long", entry=100, sl=95, tp=tp, high=high, low=99, last=128)
        assert result is not None
        assert result["status"] == "tp_hit"
        assert result["close_price"] == tp
        assert result["pnl_pct"] == pytest.approx(expected_pnl)

    def test_price_just_below_tp_no_hit(self):
        """High is 129.99, TP is 130 → no hit."""
        result = check_tp_sl("long", entry=100, sl=95, tp=130, high=129.99, low=99, last=120)
        assert result is None


# ---------------------------------------------------------------------------
# Long — both SL and TP hit same candle
# ---------------------------------------------------------------------------

class TestLongBothHit:
    """When both SL and TP are hit in the same candle, SL wins (checked first)."""

    def test_sl_wins_over_tp(self):
        """Big candle: low <= sl AND high >= tp → SL takes priority."""
        result = check_tp_sl("long", entry=100, sl=95, tp=130, high=135, low=90, last=100)
        assert result is not None
        assert result["status"] == "sl_hit"
        assert result["close_price"] == 95


# ---------------------------------------------------------------------------
# Long — no hit
# ---------------------------------------------------------------------------

class TestLongNoHit:
    """Price stays between SL and TP."""

    def test_price_between_sl_tp(self):
        result = check_tp_sl("long", entry=100, sl=95, tp=130, high=120, low=97, last=110)
        assert result is None


# ---------------------------------------------------------------------------
# Short position — SL tests
# ---------------------------------------------------------------------------

class TestShortSL:
    """Stop-loss detection for short positions."""

    @pytest.mark.parametrize(
        "high, sl, expected_pnl",
        [
            (105.0, 105.0, -5.0),    # exact touch
            (110.0, 105.0, -5.0),    # gap through SL
            (105.01, 105.0, -5.0),   # high above SL
        ],
        ids=["exact-touch", "gap-through", "above-sl"],
    )
    def test_sl_hit(self, high, sl, expected_pnl):
        result = check_tp_sl("short", entry=100, sl=sl, tp=70, high=high, low=98, last=104)
        assert result is not None
        assert result["status"] == "sl_hit"
        assert result["close_price"] == sl
        assert result["pnl_pct"] == pytest.approx(expected_pnl)

    def test_price_just_below_sl_no_hit(self):
        """High is 104.99, SL is 105 → no hit (104.99 < 105)."""
        result = check_tp_sl("short", entry=100, sl=105, tp=70, high=104.99, low=95, last=98)
        assert result is None


# ---------------------------------------------------------------------------
# Short position — TP tests
# ---------------------------------------------------------------------------

class TestShortTP:
    """Take-profit detection for short positions."""

    @pytest.mark.parametrize(
        "low, tp, expected_pnl",
        [
            (70.0, 70.0, 30.0),     # exact touch
            (65.0, 70.0, 30.0),     # gap through TP
        ],
        ids=["exact-touch", "gap-through"],
    )
    def test_tp_hit(self, low, tp, expected_pnl):
        result = check_tp_sl("short", entry=100, sl=105, tp=tp, high=101, low=low, last=72)
        assert result is not None
        assert result["status"] == "tp_hit"
        assert result["close_price"] == tp
        assert result["pnl_pct"] == pytest.approx(expected_pnl)

    def test_price_just_above_tp_no_hit(self):
        """Low is 70.01, TP is 70 → no hit."""
        result = check_tp_sl("short", entry=100, sl=105, tp=70, high=101, low=70.01, last=80)
        assert result is None


# ---------------------------------------------------------------------------
# Short — both SL and TP hit same candle
# ---------------------------------------------------------------------------

class TestShortBothHit:
    """When both SL and TP are hit in the same candle, SL wins (checked first)."""

    def test_sl_wins_over_tp(self):
        """Big candle: high >= sl AND low <= tp → SL takes priority."""
        result = check_tp_sl("short", entry=100, sl=105, tp=70, high=110, low=65, last=90)
        assert result is not None
        assert result["status"] == "sl_hit"
        assert result["close_price"] == 105


# ---------------------------------------------------------------------------
# Short — no hit
# ---------------------------------------------------------------------------

class TestShortNoHit:
    """Price stays between TP and SL for short."""

    def test_price_between_tp_sl(self):
        result = check_tp_sl("short", entry=100, sl=105, tp=70, high=103, low=75, last=90)
        assert result is None


# ---------------------------------------------------------------------------
# PnL calculations — parametrized
# ---------------------------------------------------------------------------

class TestPnLCalculations:
    """Verify PnL percentage math for various scenarios."""

    @pytest.mark.parametrize(
        "direction, entry, close_price, expected_pnl",
        [
            # Long wins
            ("long", 100, 110, 10.0),
            ("long", 100, 200, 100.0),
            ("long", 50, 75, 50.0),
            # Long losses
            ("long", 100, 90, -10.0),
            ("long", 100, 50, -50.0),
            # Short wins
            ("short", 100, 90, 10.0),
            ("short", 100, 50, 50.0),
            ("short", 200, 100, 50.0),
            # Short losses
            ("short", 100, 110, -10.0),
            ("short", 100, 150, -50.0),
            # Penny coins (large percentage moves)
            ("long", 0.001, 0.002, 100.0),
            ("short", 0.001, 0.0005, 50.0),
            # High-priced assets
            ("long", 60000, 66000, 10.0),
            ("short", 60000, 54000, 10.0),
        ],
        ids=[
            "long-win-10pct",
            "long-win-100pct",
            "long-win-50pct",
            "long-loss-10pct",
            "long-loss-50pct",
            "short-win-10pct",
            "short-win-50pct",
            "short-win-50pct-200",
            "short-loss-10pct",
            "short-loss-50pct",
            "penny-long-double",
            "penny-short-50pct",
            "btc-long-10pct",
            "btc-short-10pct",
        ],
    )
    def test_pnl_formula(self, direction, entry, close_price, expected_pnl):
        """Verify PnL calculation with known close_price.

        We set up SL/TP so the close_price is hit as expected.
        """
        is_long = direction == "long"

        if expected_pnl > 0:
            # Winning trade: hit TP
            if is_long:
                tp = close_price
                sl = entry * 0.5  # far away, won't hit
                result = check_tp_sl(direction, entry, sl, tp, high=close_price, low=entry * 0.9, last=close_price)
            else:
                tp = close_price
                sl = entry * 2.0
                result = check_tp_sl(direction, entry, sl, tp, high=entry * 1.1, low=close_price, last=close_price)
        else:
            # Losing trade: hit SL
            if is_long:
                sl = close_price
                tp = entry * 2.0
                result = check_tp_sl(direction, entry, sl, tp, high=entry * 1.1, low=close_price, last=close_price)
            else:
                sl = close_price
                tp = entry * 0.5
                result = check_tp_sl(direction, entry, sl, tp, high=close_price, low=entry * 0.9, last=close_price)

        assert result is not None
        assert result["pnl_pct"] == pytest.approx(expected_pnl, rel=1e-9)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Boundary and unusual scenarios."""

    def test_entry_equals_sl_long(self):
        """Entry == SL: immediate SL hit with 0% PnL."""
        result = check_tp_sl("long", entry=100, sl=100, tp=130, high=110, low=100, last=105)
        assert result is not None
        assert result["status"] == "sl_hit"
        assert result["pnl_pct"] == pytest.approx(0.0)

    def test_entry_equals_tp_long(self):
        """Entry == TP: immediate TP hit with 0% PnL."""
        result = check_tp_sl("long", entry=100, sl=90, tp=100, high=100, low=95, last=99)
        assert result is not None
        assert result["status"] == "tp_hit"
        assert result["pnl_pct"] == pytest.approx(0.0)

    def test_very_tight_sl_tp(self):
        """Very small difference between entry, SL, and TP (scalp trade)."""
        result = check_tp_sl("long", entry=100, sl=99.95, tp=100.10, high=100.10, low=99.96, last=100.05)
        assert result is not None
        assert result["status"] == "tp_hit"
        assert result["close_price"] == 100.10
        assert result["pnl_pct"] == pytest.approx(0.1)

    def test_flat_candle_no_movement(self):
        """High == low == last, all between SL and TP → no hit."""
        result = check_tp_sl("long", entry=100, sl=95, tp=110, high=100, low=100, last=100)
        assert result is None

    def test_close_price_is_sl_not_last_for_long(self):
        """When SL hit, close_price should be SL value, not last."""
        result = check_tp_sl("long", entry=100, sl=95, tp=130, high=110, low=94, last=97)
        assert result["close_price"] == 95  # SL, not last

    def test_close_price_is_tp_not_last_for_short(self):
        """When TP hit, close_price should be TP value, not last."""
        result = check_tp_sl("short", entry=100, sl=105, tp=80, high=102, low=78, last=85)
        assert result["close_price"] == 80  # TP, not last
