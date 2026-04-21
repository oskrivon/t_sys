"""Tests for funding capture direction logic and symbol conversion.

Validates that:
- Negative funding rate -> LONG (shorts pay longs, we collect)
- Positive funding rate -> SHORT (longs pay shorts, we collect)
- Zero / below-threshold rates are filtered out (not scheduled)
- Raw Bybit symbols are correctly converted to ccxt format
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.base import Side, StrategyConfig, StrategyType
from src.strategies.funding_capture import FundingCaptureStrategy
from src.engine.event_bus import EventBus, Event, EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(threshold_bps: float = 10.0) -> FundingCaptureStrategy:
    """Create a FundingCaptureStrategy with given threshold."""
    config = StrategyConfig(
        strategy_id="funding_test",
        name="Funding Test",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=10.0,
        max_positions=20,
        params={
            "threshold_bps": threshold_bps,
            "scan_threshold_bps": 5.0,
            "leverage": 10,
            "entry_seconds_before": 5,
            "exit_seconds_after": 30,
            "min_volume_24h": 5_000_000,
            "target_notional": 25.0,
        },
    )
    bus = EventBus()
    return FundingCaptureStrategy(config, bus)


def _make_funding_event(symbol_raw: str, funding_rate: float,
                        next_funding_ms: int = 9999999999999,
                        last_price: float = 100.0) -> Event:
    """Create a mock FUNDING_RATE event."""
    return Event(
        type=EventType.FUNDING_RATE,
        data={
            "_symbol_raw": symbol_raw,
            "fundingRate": funding_rate,
            "nextFundingTime": next_funding_ms,
            "lastPrice": last_price,
        },
    )


# ---------------------------------------------------------------------------
# Direction logic tests
# ---------------------------------------------------------------------------

class TestFundingDirection:
    """Test direction selection: negative rate -> LONG, positive -> SHORT."""

    async def test_negative_rate_gives_long(self):
        """Negative funding = shorts pay longs -> go LONG to receive."""
        strat = _make_strategy(threshold_bps=10.0)
        strat._monitored.add("BTCUSDT")

        await strat._on_funding_update(_make_funding_event("BTCUSDT", -0.002))

        opp = strat._opportunities["BTCUSDT"]
        assert opp.direction == Side.LONG

    async def test_positive_rate_gives_short(self):
        """Positive funding = longs pay shorts -> go SHORT to receive."""
        strat = _make_strategy(threshold_bps=10.0)
        strat._monitored.add("ETHUSDT")

        await strat._on_funding_update(_make_funding_event("ETHUSDT", 0.003))

        opp = strat._opportunities["ETHUSDT"]
        assert opp.direction == Side.SHORT

    async def test_zero_rate_filtered_out(self):
        """Zero funding rate -> early return, no opportunity recorded."""
        strat = _make_strategy(threshold_bps=10.0)
        strat._monitored.add("BTCUSDT")

        await strat._on_funding_update(_make_funding_event("BTCUSDT", 0.0))

        assert "BTCUSDT" not in strat._opportunities

    async def test_none_rate_filtered_out(self):
        """None / missing funding rate -> early return."""
        strat = _make_strategy(threshold_bps=10.0)
        strat._monitored.add("BTCUSDT")

        event = Event(
            type=EventType.FUNDING_RATE,
            data={
                "_symbol_raw": "BTCUSDT",
                "fundingRate": None,
                "nextFundingTime": 9999999999999,
                "lastPrice": 50000.0,
            },
        )
        await strat._on_funding_update(event)

        assert "BTCUSDT" not in strat._opportunities

    async def test_unmonitored_symbol_ignored(self):
        """Symbol not in _monitored set -> early return."""
        strat = _make_strategy()
        # Don't add RANDUSDT to monitored

        await strat._on_funding_update(_make_funding_event("RANDUSDT", -0.01))

        assert "RANDUSDT" not in strat._opportunities


# ---------------------------------------------------------------------------
# Threshold scheduling tests
# ---------------------------------------------------------------------------

class TestFundingThreshold:
    """Test that rates below threshold are not scheduled for entry."""

    async def test_below_threshold_not_scheduled(self):
        """Rate below threshold_bps -> opportunity stored but not scheduled."""
        strat = _make_strategy(threshold_bps=15.0)  # 15bps = 0.0015
        strat._monitored.add("BTCUSDT")

        # 10bps = 0.001, below 15bps threshold
        await strat._on_funding_update(_make_funding_event("BTCUSDT", -0.001))

        # Opportunity is still recorded (for monitoring)
        assert "BTCUSDT" in strat._opportunities
        # But not scheduled for entry
        assert "BTCUSDT" not in strat._scheduled

    async def test_at_threshold_opportunity_recorded(self):
        """Rate exactly at threshold -> opportunity stored with correct direction."""
        strat = _make_strategy(threshold_bps=10.0)  # 10bps = 0.001
        strat._monitored.add("BTCUSDT")

        await strat._on_funding_update(_make_funding_event("BTCUSDT", -0.001))

        opp = strat._opportunities["BTCUSDT"]
        assert opp.direction == Side.LONG
        assert opp.funding_rate == -0.001

    async def test_very_small_negative_not_scheduled(self):
        """Very small negative rate (1bps) below threshold -> not scheduled."""
        strat = _make_strategy(threshold_bps=10.0)
        strat._monitored.add("SOLUSDT")

        await strat._on_funding_update(_make_funding_event("SOLUSDT", -0.0001))

        assert "SOLUSDT" in strat._opportunities
        assert "SOLUSDT" not in strat._scheduled


# ---------------------------------------------------------------------------
# Symbol conversion tests
# ---------------------------------------------------------------------------

class TestSymbolConversion:
    """Test BTCUSDT -> BTC/USDT:USDT conversion logic."""

    @pytest.mark.parametrize("raw,expected", [
        ("BTCUSDT", "BTC/USDT:USDT"),
        ("ETHUSDT", "ETH/USDT:USDT"),
        ("SOLUSDT", "SOL/USDT:USDT"),
        ("XRPUSDT", "XRP/USDT:USDT"),
        ("DOGEUSDT", "DOGE/USDT:USDT"),
        ("SUSHIUSDT", "SUSHI/USDT:USDT"),
        ("1000PEPEUSDT", "1000PEPE/USDT:USDT"),
        ("BNBUSDT", "BNB/USDT:USDT"),
        ("AVAXUSDT", "AVAX/USDT:USDT"),
    ])
    async def test_symbol_conversion(self, raw: str, expected: str):
        """Raw Bybit symbol is converted to ccxt perpetual format."""
        strat = _make_strategy()
        strat._monitored.add(raw)

        await strat._on_funding_update(_make_funding_event(raw, -0.002))

        opp = strat._opportunities[raw]
        assert opp.symbol_ccxt == expected

    async def test_sushi_no_false_replace(self):
        """SUSHIUSDT must not have USDT stripped from 'SUSHI' part.

        The replace('USDT', '') only removes the trailing USDT because
        'SUSHI' does not contain 'USDT' as a substring.
        """
        strat = _make_strategy()
        strat._monitored.add("SUSHIUSDT")

        await strat._on_funding_update(_make_funding_event("SUSHIUSDT", 0.005))

        opp = strat._opportunities["SUSHIUSDT"]
        assert opp.symbol_ccxt == "SUSHI/USDT:USDT"

    async def test_digits_in_symbol(self):
        """Symbols with digits (1000PEPE) must not break conversion."""
        strat = _make_strategy()
        strat._monitored.add("1000PEPEUSDT")

        await strat._on_funding_update(
            _make_funding_event("1000PEPEUSDT", -0.01, last_price=0.001)
        )

        opp = strat._opportunities["1000PEPEUSDT"]
        assert opp.symbol_ccxt == "1000PEPE/USDT:USDT"
        assert opp.direction == Side.LONG
