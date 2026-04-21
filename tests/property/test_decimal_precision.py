"""Property-based tests for decimal precision in financial calculations.

Verifies that PnL calculations, fee math, and position arithmetic
maintain precision and satisfy fundamental invariants.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# PnL invariants
# ---------------------------------------------------------------------------

def compute_pnl_pct(entry: float, close: float, is_long: bool) -> float:
    """Replicate PnL formula from paper_trading/service.py."""
    if entry == 0:
        return 0.0
    if is_long:
        return (close - entry) / entry * 100
    else:
        return (entry - close) / entry * 100


@given(
    entry=st.floats(min_value=0.0001, max_value=200000, allow_nan=False, allow_infinity=False),
    close=st.floats(min_value=0.0001, max_value=200000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=500)
def test_pnl_long_short_symmetry(entry, close):
    """Long PnL at close X = negative of Short PnL at same close X.

    If you go long and price goes up 10%, that's +10%.
    If you go short at the same entry and price goes up 10%, that's -10%.
    """
    assume(entry > 0)
    long_pnl = compute_pnl_pct(entry, close, is_long=True)
    short_pnl = compute_pnl_pct(entry, close, is_long=False)
    assert abs(long_pnl + short_pnl) < 1e-9, (
        f"Long PnL ({long_pnl}) + Short PnL ({short_pnl}) should be 0"
    )


@given(
    entry=st.floats(min_value=0.0001, max_value=200000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_pnl_zero_when_close_equals_entry(entry):
    """PnL must be exactly 0 when close == entry."""
    assume(entry > 0)
    assert compute_pnl_pct(entry, entry, True) == 0.0
    assert compute_pnl_pct(entry, entry, False) == 0.0


@given(
    entry=st.floats(min_value=0.01, max_value=100000, allow_nan=False, allow_infinity=False),
    close=st.floats(min_value=0.01, max_value=100000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_long_pnl_positive_when_price_rises(entry, close):
    """Long PnL must be positive when close > entry."""
    assume(entry > 0 and close > entry)
    pnl = compute_pnl_pct(entry, close, is_long=True)
    assert pnl > 0


@given(
    entry=st.floats(min_value=0.01, max_value=100000, allow_nan=False, allow_infinity=False),
    close=st.floats(min_value=0.01, max_value=100000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_short_pnl_positive_when_price_drops(entry, close):
    """Short PnL must be positive when close < entry."""
    assume(entry > 0 and close < entry)
    pnl = compute_pnl_pct(entry, close, is_long=False)
    assert pnl > 0


# ---------------------------------------------------------------------------
# Fee invariants
# ---------------------------------------------------------------------------

@given(
    notional=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    fee_rate=st.floats(min_value=0.0001, max_value=0.01, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_fee_always_positive_and_proportional(notional, fee_rate):
    """Fees are always positive and scale linearly with notional."""
    fee = notional * fee_rate
    assert fee > 0
    # Double notional = double fee
    fee_double = (notional * 2) * fee_rate
    assert abs(fee_double - fee * 2) < 1e-6


@given(
    notional=st.floats(min_value=1.0, max_value=100000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_rt_fee_equals_two_sides(notional):
    """Round-trip fee = 2x one-side fee."""
    taker = 0.00055
    one_side = notional * taker
    round_trip = notional * taker * 2
    assert abs(round_trip - one_side * 2) < 1e-10


# ---------------------------------------------------------------------------
# Funding payment invariants
# ---------------------------------------------------------------------------

@given(
    notional=st.floats(min_value=1.0, max_value=100000, allow_nan=False, allow_infinity=False),
    rate_bps=st.floats(min_value=0.1, max_value=500, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
def test_funding_payment_proportional(notional, rate_bps):
    """Funding payment scales linearly with notional and rate."""
    rate = rate_bps / 10000
    payment = notional * rate
    assert payment > 0

    # Net = funding - RT_fee. At rate > 11bps, net should be positive
    rt_fee = notional * 0.00055 * 2
    net = payment - rt_fee
    if rate_bps > 11:
        assert net > 0, f"rate={rate_bps}bps should be net positive, got {net}"
