"""Shared test fixtures for trading infrastructure tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.models.base import OrderSide, OrderType
from src.engine.event_bus import EventBus, Event, EventType
from src.execution.position_tracker import PositionTracker, LivePosition
from src.strategies.base import (
    Side, StrategyConfig, StrategyType, TradeSignal, TargetPosition,
)
from src.strategy.models import Level, SignalType


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def collecting_bus():
    """EventBus that collects all published events for assertion."""
    bus = EventBus()
    bus._collected: list[Event] = []

    original_publish = bus.publish

    async def _collect_and_publish(event: Event):
        bus._collected.append(event)
        await original_publish(event)

    bus.publish = _collect_and_publish
    return bus


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------

@pytest.fixture
def position_tracker():
    return PositionTracker()


# ---------------------------------------------------------------------------
# Sample positions
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_long_position():
    return LivePosition(
        symbol="BTC/USDT:USDT",
        side="long",
        qty=Decimal("0.001"),
        entry_price=Decimal("50000"),
        leverage=10,
        strategy_id="funding_capture",
    )


@pytest.fixture
def sample_short_position():
    return LivePosition(
        symbol="ETH/USDT:USDT",
        side="short",
        qty=Decimal("0.01"),
        entry_price=Decimal("3000"),
        leverage=10,
        strategy_id="funding_capture",
    )


# ---------------------------------------------------------------------------
# Strategy configs
# ---------------------------------------------------------------------------

@pytest.fixture
def funding_config():
    return StrategyConfig(
        strategy_id="funding_capture",
        name="Funding Capture",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=10.0,
        max_positions=20,
        params={
            "threshold_bps": 15.0,
            "scan_threshold_bps": 10.0,
            "leverage": 10,
            "entry_seconds_before": 5,
            "exit_seconds_after": 30,
            "min_volume_24h": 5_000_000,
            "target_notional": 25.0,
        },
    )


# ---------------------------------------------------------------------------
# Trade signals
# ---------------------------------------------------------------------------

@pytest.fixture
def long_funding_signal():
    return TradeSignal(
        strategy_id="funding_capture",
        symbol="BTC/USDT:USDT",
        side=Side.LONG,
        entry_price=0.0,
        sl=0.0,
        tp=0.0,
        confidence=0.8,
        metadata={
            "type": "funding_capture",
            "funding_rate": -0.002,
            "funding_rate_bps": 20.0,
            "next_funding_time": 1700000000000,
            "leverage": 10,
            "exit_after_seconds": 30,
            "target_notional": 25.0,
            "last_price": 50000.0,
        },
        timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )


@pytest.fixture
def short_funding_signal():
    return TradeSignal(
        strategy_id="funding_capture",
        symbol="ETH/USDT:USDT",
        side=Side.SHORT,
        entry_price=0.0,
        sl=0.0,
        tp=0.0,
        confidence=0.6,
        metadata={
            "type": "funding_capture",
            "funding_rate": 0.003,
            "funding_rate_bps": 30.0,
            "next_funding_time": 1700000000000,
            "leverage": 10,
            "exit_after_seconds": 30,
            "target_notional": 25.0,
            "last_price": 3000.0,
        },
        timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# S/R Levels
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_level():
    return Level(
        price=50000.0,
        touches=3,
        zone_high=50100.0,
        zone_low=49900.0,
        first_idx=10,
        last_idx=80,
        score=5,
    )


@pytest.fixture
def tight_level():
    """Level with very tight zone (< 0.3% of price)."""
    return Level(
        price=100.0,
        touches=2,
        zone_high=100.05,
        zone_low=99.95,
        first_idx=10,
        last_idx=50,
        score=3,
    )


# ---------------------------------------------------------------------------
# Mock exchange (CCXT)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ccxt_client():
    """Mock raw ccxt.bybit client."""
    client = AsyncMock()

    # market() returns market info
    client.market.return_value = {
        "limits": {"amount": {"min": 0.001}},
        "precision": {"amount": 0.001},
    }

    # fetch_ticker
    client.fetch_ticker.return_value = {
        "last": 50000.0,
        "close": 50000.0,
        "bid": 49999.0,
        "ask": 50001.0,
        "high": 51000.0,
        "low": 49000.0,
    }

    # create_order
    client.create_order.return_value = {
        "id": "order_123",
        "average": 50000.0,
        "price": 50000.0,
        "amount": 0.001,
        "status": "closed",
        "side": "buy",
    }

    # fetch_tickers (for funding scan)
    client.fetch_tickers.return_value = {}

    # load_markets
    client.load_markets.return_value = {}

    return client


@pytest.fixture
def mock_exchange(mock_ccxt_client):
    """Mock CCXTAdapter with underlying client."""
    exchange = AsyncMock()
    exchange.client = mock_ccxt_client
    exchange.set_leverage = AsyncMock()
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.create_order = AsyncMock()
    return exchange
