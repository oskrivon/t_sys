"""Property-based tests for position sizing invariants.

Uses Hypothesis to generate random inputs and verify that
critical financial invariants always hold.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.execution.executor import ExecutionManager
from src.execution.position_tracker import PositionTracker, LivePosition
from src.engine.event_bus import EventBus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_executor(min_qty=0.001, qty_step=0.001, price=50000.0):
    """Create ExecutionManager with mocked exchange returning given market params."""
    exchange = AsyncMock()
    exchange.client = MagicMock()
    exchange.client.market.return_value = {
        "limits": {"amount": {"min": min_qty}},
        "precision": {"amount": qty_step},
    }
    exchange.client.fetch_ticker = AsyncMock(return_value={
        "last": price, "close": price,
    })
    bus = EventBus()
    positions = PositionTracker()
    return ExecutionManager(exchange, bus, positions)


# ---------------------------------------------------------------------------
# Property: qty is always positive and >= min_qty
# ---------------------------------------------------------------------------

@given(
    price=st.floats(min_value=0.0001, max_value=200000, allow_nan=False, allow_infinity=False),
    notional=st.floats(min_value=0.1, max_value=100000, allow_nan=False, allow_infinity=False),
    min_qty=st.floats(min_value=0.0001, max_value=100, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_qty_always_positive_and_above_min(price, notional, min_qty):
    """For any valid price/notional, computed qty is always > 0 and >= min_qty."""
    assume(price > 0 and notional > 0 and min_qty > 0)

    executor = make_executor(min_qty=min_qty, qty_step=min_qty, price=price)
    qty = await executor._compute_qty("TEST/USDT:USDT", notional)

    assert qty > 0, f"qty must be positive, got {qty}"
    assert qty >= Decimal(str(min_qty)), f"qty {qty} < min_qty {min_qty}"


# ---------------------------------------------------------------------------
# Property: effective notional never exceeds target (with fee margin)
# ---------------------------------------------------------------------------

@given(
    price=st.floats(min_value=1.0, max_value=100000, allow_nan=False, allow_infinity=False),
    notional=st.floats(min_value=10.0, max_value=50000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_notional_within_budget(price, notional):
    """Computed qty * price should not significantly exceed target notional."""
    assume(price > 0 and notional > 0)

    executor = make_executor(min_qty=0.001, qty_step=0.001, price=price)
    qty = await executor._compute_qty("TEST/USDT:USDT", notional)

    actual_notional = float(qty) * price
    # Allow 1% overshoot for rounding + min_qty enforcement
    assert actual_notional <= notional * 1.01 or float(qty) == 0.001, (
        f"notional {actual_notional} exceeds target {notional} by more than 1%"
    )


# ---------------------------------------------------------------------------
# Property: qty is always a multiple of qty_step
# ---------------------------------------------------------------------------

@given(
    price=st.floats(min_value=0.01, max_value=100000, allow_nan=False, allow_infinity=False),
    notional=st.floats(min_value=1.0, max_value=10000, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_qty_respects_step_size(price, notional):
    """Qty should be a multiple of qty_step (exchange requirement)."""
    assume(price > 0 and notional > 0)

    step = 0.01
    executor = make_executor(min_qty=0.01, qty_step=step, price=price)
    qty = await executor._compute_qty("TEST/USDT:USDT", notional)

    remainder = float(qty) % step
    assert remainder < 1e-9 or abs(remainder - step) < 1e-9, (
        f"qty {qty} is not a multiple of step {step}, remainder={remainder}"
    )


# ---------------------------------------------------------------------------
# Property: zero/negative notional returns min_qty
# ---------------------------------------------------------------------------

@given(
    notional=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_zero_notional_returns_min_qty(notional):
    """Zero or negative target_notional should return exchange minimum."""
    executor = make_executor(min_qty=0.5, price=100.0)
    qty = await executor._compute_qty("TEST/USDT:USDT", notional)
    assert qty == Decimal("0.5")


# ---------------------------------------------------------------------------
# Property: LivePosition notional and margin are always non-negative
# ---------------------------------------------------------------------------

@given(
    qty=st.decimals(min_value="0.001", max_value="1000000", places=8),
    price=st.decimals(min_value="0.0001", max_value="200000", places=8),
    leverage=st.integers(min_value=1, max_value=125),
)
@settings(max_examples=200)
def test_position_notional_and_margin_non_negative(qty, price, leverage):
    """Position notional and margin must always be >= 0."""
    pos = LivePosition(
        symbol="TEST/USDT:USDT",
        side="long",
        qty=qty,
        entry_price=price,
        leverage=leverage,
    )
    assert pos.notional >= 0
    assert pos.margin >= 0
    assert pos.margin <= pos.notional


# ---------------------------------------------------------------------------
# Property: margin = notional / leverage (exact)
# ---------------------------------------------------------------------------

@given(
    qty=st.decimals(min_value="0.01", max_value="10000", places=4),
    price=st.decimals(min_value="0.01", max_value="100000", places=4),
    leverage=st.integers(min_value=1, max_value=125),
)
@settings(max_examples=200)
def test_margin_equals_notional_div_leverage(qty, price, leverage):
    """Margin must exactly equal notional / leverage."""
    pos = LivePosition(
        symbol="TEST/USDT:USDT",
        side="long",
        qty=qty,
        entry_price=price,
        leverage=leverage,
    )
    expected = pos.notional / leverage
    assert pos.margin == expected
