"""Tests for ExecutionManager._compute_qty order quantity calculation.

Validates:
- target_notional=0 returns min_qty
- Correct qty for normal and cheap coins
- Rounding down to qty_step
- Result never below min_qty
- Fallback when price=0 or "last" is missing
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.executor import ExecutionManager
from src.execution.position_tracker import PositionTracker
from src.engine.event_bus import EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(
    market_info: dict | None = None,
    ticker_info: dict | None = None,
) -> ExecutionManager:
    """Build an ExecutionManager with mocked exchange."""
    mock_client = MagicMock()

    default_market = {
        "limits": {"amount": {"min": 0.001}},
        "precision": {"amount": 0.001},
    }
    mock_client.market.return_value = market_info or default_market

    default_ticker = {"last": 50000.0, "close": 50000.0}
    # fetch_ticker is async, so use AsyncMock for it
    mock_client.fetch_ticker = AsyncMock(return_value=ticker_info or default_ticker)

    exchange = AsyncMock()
    exchange.client = mock_client

    bus = EventBus()
    positions = PositionTracker()

    return ExecutionManager(exchange, bus, positions, notifier=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestComputeQty:
    """Test _compute_qty calculation logic."""

    async def test_zero_notional_returns_min_qty(self):
        """target_notional=0 -> returns min_qty directly."""
        executor = _make_executor()

        qty = await executor._compute_qty("BTC/USDT:USDT", target_notional=0)

        assert qty == Decimal("0.001")

    async def test_default_notional_returns_min_qty(self):
        """No target_notional arg (defaults to 0) -> returns min_qty."""
        executor = _make_executor()

        qty = await executor._compute_qty("BTC/USDT:USDT")

        assert qty == Decimal("0.001")

    async def test_btc_normal_calculation(self):
        """$25 notional at $50,000 BTC -> 0.0005 BTC (rounded to step=0.001? -> 0.0)
        but max(0.0, 0.001) -> 0.001 (min_qty)."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 0.001}},
                "precision": {"amount": 0.001},
            },
            ticker_info={"last": 50000.0, "close": 50000.0},
        )

        qty = await executor._compute_qty("BTC/USDT:USDT", target_notional=25)

        # 25/50000 = 0.0005; int(0.0005/0.001)*0.001 = 0.0; max(0.0, 0.001) = 0.001
        assert qty == Decimal("0.001")

    async def test_btc_larger_notional(self):
        """$500 notional at $50,000 -> 0.009 BTC (after RT fee deduction)."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 0.001}},
                "precision": {"amount": 0.001},
            },
            ticker_info={"last": 50000.0, "close": 50000.0},
        )

        qty = await executor._compute_qty("BTC/USDT:USDT", target_notional=500)

        # effective = 500 * 0.9989 = 499.45; 499.45/50000 = 0.009989
        # int(0.009989/0.001)*0.001 = 9*0.001 = 0.009; max(0.009, 0.001) = 0.009
        assert qty == Decimal(str(int(500 * 0.9989 / 50000 / 0.001) * 0.001))

    async def test_cheap_coin_large_qty(self):
        """$25 notional at $0.001 price -> 24972 units (after RT fee deduction)."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 1}},
                "precision": {"amount": 1},
            },
            ticker_info={"last": 0.001, "close": 0.001},
        )

        qty = await executor._compute_qty("PEPE/USDT:USDT", target_notional=25)

        # effective = 25 * 0.9989 = 24.9725; 24.9725/0.001 = 24972.5
        # int(24972.5/1)*1 = 24972; max(24972, 1) = 24972
        assert qty == Decimal("24972")

    async def test_rounding_down_to_step(self):
        """raw_qty=0.555, step=0.01 -> rounds down to 0.55."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 0.01}},
                "precision": {"amount": 0.01},
            },
            # notional=55.5, price=100 -> raw=0.555
            ticker_info={"last": 100.0, "close": 100.0},
        )

        qty = await executor._compute_qty("SOL/USDT:USDT", target_notional=55.5)

        # 55.5/100 = 0.555; int(0.555/0.01)*0.01 = int(55.5)*0.01 = 55*0.01 = 0.55
        assert qty == Decimal("0.55")

    async def test_result_never_below_min_qty(self):
        """When rounded qty < min_qty, result is clamped to min_qty."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 0.1}},
                "precision": {"amount": 0.1},
            },
            ticker_info={"last": 1000.0, "close": 1000.0},
        )

        qty = await executor._compute_qty("ETH/USDT:USDT", target_notional=5)

        # 5/1000 = 0.005; int(0.005/0.1)*0.1 = 0.0; max(0.0, 0.1) = 0.1
        assert qty == Decimal("0.1")

    async def test_price_zero_falls_back_to_min_qty(self):
        """Ticker returns price=0 -> falls back to min_qty."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 0.001}},
                "precision": {"amount": 0.001},
            },
            ticker_info={"last": 0, "close": 0},
        )

        qty = await executor._compute_qty("BTC/USDT:USDT", target_notional=25)

        assert qty == Decimal("0.001")

    async def test_missing_last_uses_close(self):
        """Ticker has no "last" key -> falls back to "close"."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 1}},
                "precision": {"amount": 1},
            },
            ticker_info={"close": 2.0},  # no "last"
        )

        qty = await executor._compute_qty("XRP/USDT:USDT", target_notional=100)

        # effective = 100 * 0.9989 = 99.89; 99.89/2 = 49.945
        # int(49.945/1)*1 = 49; max(49, 1) = 49
        assert qty == Decimal("49")

    async def test_missing_last_none_uses_close(self):
        """Ticker has last=None explicitly -> falls back to "close"."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": 1}},
                "precision": {"amount": 1},
            },
            ticker_info={"last": None, "close": 5.0},
        )

        qty = await executor._compute_qty("DOGE/USDT:USDT", target_notional=50)

        # effective = 50 * 0.9989 = 49.945; 49.945/5 = 9.989
        # int(9.989/1)*1 = 9; max(9, 1) = 9
        assert qty == Decimal("9")

    async def test_missing_limits_defaults_to_one(self):
        """Market info missing limits -> min_qty defaults to 1."""
        executor = _make_executor(
            market_info={
                "precision": {"amount": 1},
            },
            ticker_info={"last": 10.0, "close": 10.0},
        )

        qty = await executor._compute_qty("TOKEN/USDT:USDT", target_notional=25)

        # min_qty=1, step=1, 25/10=2.5, int(2.5/1)*1=2, max(2,1)=2
        assert qty == Decimal("2")

    @pytest.mark.parametrize("notional,price,step,min_q,expected", [
        # effective = notional * (1 - 0.00055*2) = notional * 0.9989
        (100, 50.0, 0.1, 0.1, None),         # 99.89/50=1.9978, floor 0.1 → 1.9
        (100, 33.0, 0.1, 0.1, "3.0"),        # 99.89/33=3.027, floor → 3.0
        (10, 3.0, 0.5, 0.5, "3.0"),          # 9.989/3=3.3297, floor → 3.0
        (1, 10000.0, 0.0001, 0.0001, "0.0001"),  # 0.9989/10000=0.00009989, floor → 0.0, max → 0.0001
    ])
    async def test_parametrized_qty(self, notional, price, step, min_q, expected):
        """Parametrized quantity calculations (with RT fee deduction)."""
        executor = _make_executor(
            market_info={
                "limits": {"amount": {"min": min_q}},
                "precision": {"amount": step},
            },
            ticker_info={"last": price, "close": price},
        )

        qty = await executor._compute_qty("X/USDT:USDT", target_notional=notional)

        if expected is None:
            # Compute expected dynamically to avoid float repr issues
            effective = notional * (1 - 0.00055 * 2)
            raw = int(effective / price / step) * step
            expected_qty = Decimal(str(max(raw, min_q)))
            assert qty == expected_qty
        else:
            assert qty == Decimal(expected)
