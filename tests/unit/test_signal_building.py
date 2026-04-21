"""Tests for build_signal in src/strategy/signals.py.

Verifies SL/TP calculation, zone width fallback, risk validation,
and correct handling of all signal types.
"""
from __future__ import annotations

import pytest

from src.strategy.models import Level, Signal, SignalType
from src.strategy.signals import build_signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_level(
    price: float = 50_000.0,
    zone_high: float = 50_100.0,
    zone_low: float = 49_900.0,
    score: int = 5,
) -> Level:
    return Level(
        price=price,
        touches=3,
        zone_high=zone_high,
        zone_low=zone_low,
        first_idx=10,
        last_idx=80,
        score=score,
    )


# ---------------------------------------------------------------------------
# Long signal basics
# ---------------------------------------------------------------------------

class TestLongSignal:
    """Long signal: SL below zone_low, TP above entry."""

    def test_sl_below_zone_low(self):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0)
        assert sig is not None
        assert sig.sl < level.zone_low, "SL must sit below zone_low"

    def test_tp_above_entry(self):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0)
        assert sig is not None
        assert sig.tp > sig.entry_price, "TP must be above entry for longs"

    def test_is_long_flag(self):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0)
        assert sig is not None
        assert sig.is_long is True

    def test_rr_ratio_default(self):
        """Default RR ratio is 3.0."""
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0)
        assert sig is not None
        assert abs(sig.rr_ratio - 3.0) < 0.01

    def test_sl_calculation_exact(self):
        """SL = zone_low - zone_width * 0.3 for a normal (non-tight) zone."""
        level = _make_level(zone_high=50_100.0, zone_low=49_900.0)
        zone_w = 50_100.0 - 49_900.0  # 200
        expected_sl = 49_900.0 - zone_w * 0.3  # 49840
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0)
        assert sig is not None
        assert abs(sig.sl - expected_sl) < 1e-6


# ---------------------------------------------------------------------------
# Short signal basics
# ---------------------------------------------------------------------------

class TestShortSignal:
    """Short signal: SL above zone_high, TP below entry."""

    def test_sl_above_zone_high(self):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.SHORT_RETEST, level, 49_950.0)
        assert sig is not None
        assert sig.sl > level.zone_high, "SL must sit above zone_high"

    def test_tp_below_entry(self):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.SHORT_RETEST, level, 49_950.0)
        assert sig is not None
        assert sig.tp < sig.entry_price, "TP must be below entry for shorts"

    def test_is_long_flag(self):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.SHORT_RETEST, level, 49_950.0)
        assert sig is not None
        assert sig.is_long is False

    def test_sl_calculation_exact(self):
        """SL = zone_high + zone_width * 0.3 for shorts."""
        level = _make_level(zone_high=50_100.0, zone_low=49_900.0)
        zone_w = 200.0
        expected_sl = 50_100.0 + zone_w * 0.3  # 50160
        sig = build_signal("BTC/USDT:USDT", SignalType.SHORT_RETEST, level, 49_950.0)
        assert sig is not None
        assert abs(sig.sl - expected_sl) < 1e-6


# ---------------------------------------------------------------------------
# Tight zone fallback
# ---------------------------------------------------------------------------

class TestTightZoneFallback:
    """When zone_width < 0.3% of entry, fallback to 0.5% of entry."""

    def test_tight_zone_uses_fallback_width(self):
        """Zone 0.1% wide at entry=100 → zone_w replaced by 0.5 (0.5% of 100)."""
        level = _make_level(price=100.0, zone_high=100.05, zone_low=99.95)
        # Original zone_w = 0.10, threshold = 100 * 0.003 = 0.30 → triggers fallback
        fallback_w = 100.0 * 0.005  # 0.50
        expected_sl = 99.95 - fallback_w * 0.3  # 99.80
        sig = build_signal("X/USDT:USDT", SignalType.LONG_RETEST, level, 100.0)
        assert sig is not None
        assert abs(sig.sl - expected_sl) < 1e-6

    def test_normal_zone_no_fallback(self):
        """Zone 0.4% wide at entry=50000 → no fallback."""
        level = _make_level(zone_high=50_100.0, zone_low=49_900.0)
        # zone_w = 200, threshold = 50000 * 0.003 = 150 → 200 >= 150, no fallback
        expected_sl = 49_900.0 - 200.0 * 0.3
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0)
        assert sig is not None
        assert abs(sig.sl - expected_sl) < 1e-6


# ---------------------------------------------------------------------------
# Risk validation — returns None
# ---------------------------------------------------------------------------

class TestRiskValidation:
    """build_signal returns None when SL distance is invalid."""

    def test_sl_distance_exceeds_8_pct(self):
        """SL > 8% of entry → None."""
        # Make a huge zone so SL is far away
        level = _make_level(zone_high=55_000.0, zone_low=45_000.0)
        # zone_w=10000, sl = 45000 - 10000*0.3 = 42000
        # sl_dist = 50000 - 42000 = 8000, 8000/50000 = 16% > 8%
        result = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_000.0)
        assert result is None

    def test_sl_distance_zero_or_negative_long(self):
        """Entry below zone_low for long → sl_dist <= 0 → None."""
        level = _make_level(zone_high=50_100.0, zone_low=49_900.0)
        # sl = 49900 - 200*0.3 = 49840, entry=49_800 → sl_dist = 49800 - 49840 = -40
        result = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 49_800.0)
        assert result is None

    def test_sl_distance_zero_or_negative_short(self):
        """Entry above zone_high for short → sl_dist <= 0 → None."""
        level = _make_level(zone_high=50_100.0, zone_low=49_900.0)
        # sl = 50100 + 200*0.3 = 50160, entry=50_200 → sl_dist = 50160 - 50200 = -40
        result = build_signal("BTC/USDT:USDT", SignalType.SHORT_RETEST, level, 50_200.0)
        assert result is None


# ---------------------------------------------------------------------------
# Custom RR ratio
# ---------------------------------------------------------------------------

class TestCustomRRRatio:
    """RR ratio can be overridden from the default 3.0."""

    @pytest.mark.parametrize("rr", [1.0, 2.0, 4.5])
    def test_custom_rr_long(self, rr: float):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.LONG_RETEST, level, 50_050.0, rr_ratio=rr)
        assert sig is not None
        assert abs(sig.rr_ratio - rr) < 0.01

    @pytest.mark.parametrize("rr", [1.0, 2.0, 4.5])
    def test_custom_rr_short(self, rr: float):
        level = _make_level()
        sig = build_signal("BTC/USDT:USDT", SignalType.SHORT_RETEST, level, 49_950.0, rr_ratio=rr)
        assert sig is not None
        assert abs(sig.rr_ratio - rr) < 0.01


# ---------------------------------------------------------------------------
# is_long mapping for all SignalType values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "signal_type, expected_is_long",
    [
        (SignalType.LONG_RETEST, True),
        (SignalType.LONG_ZAKOL, True),
        (SignalType.SHORT_RETEST, False),
        (SignalType.SHORT_ZAKOL, False),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_is_long_for_all_signal_types(signal_type: SignalType, expected_is_long: bool):
    """is_long is derived from 'long' being in the signal_type.value string."""
    level = _make_level()
    entry = 50_050.0 if expected_is_long else 49_950.0
    sig = build_signal("BTC/USDT:USDT", signal_type, level, entry)
    assert sig is not None
    assert sig.is_long is expected_is_long
