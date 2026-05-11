"""Integration tests: ExecutionManager signal-to-exit pipeline.

Tests the full flow from receiving a TradeSignal event through order
placement, position tracking, and exit scheduling -- with mocked exchange.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models.base import OrderSide, OrderType
from src.engine.event_bus import EventBus, Event, EventType
from src.execution.executor import ExecutionManager
from src.execution.position_tracker import PositionTracker, LivePosition
from src.strategies.base import Side, TradeSignal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def position_tracker():
    return PositionTracker()


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_exchange(mock_ccxt_client):
    """Mock CCXTAdapter with underlying ccxt client."""
    exchange = AsyncMock()
    exchange.client = mock_ccxt_client
    exchange.set_leverage = AsyncMock()
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.create_order = AsyncMock()
    return exchange


@pytest.fixture
def mock_ccxt_client():
    from unittest.mock import MagicMock
    client = AsyncMock()
    # market() is synchronous in ccxt — use MagicMock
    client.market = MagicMock(return_value={
        "limits": {"amount": {"min": 0.001}},
        "precision": {"amount": 0.001},
    })
    client.fetch_ticker.return_value = {
        "last": 50000.0,
        "close": 50000.0,
    }
    client.create_order.return_value = {
        "id": "order_123",
        "average": 50000.0,
        "price": 50000.0,
        "amount": 0.001,
        "status": "closed",
        "side": "buy",
    }
    client.fetch_order_book.return_value = {
        "bids": [[50000.0, 1.0]],
        "asks": [[50001.0, 1.0]],
    }
    client.fetch_order.return_value = {
        "id": "order_123",
        "status": "closed",
        "average": 50000.0,
        "price": 50000.0,
        "filled": 0.001,
    }
    client.cancel_order = AsyncMock()
    return client


@pytest.fixture
async def executor(mock_exchange, event_bus, position_tracker):
    mgr = ExecutionManager(mock_exchange, event_bus, position_tracker)
    await mgr.initialize()
    return mgr


def _make_signal(
    symbol: str = "BTC/USDT:USDT",
    side: Side = Side.LONG,
    sig_type: str = "funding_capture",
    leverage: int = 10,
    target_notional: float = 25.0,
    last_price: float = 50000.0,
    funding_rate: float = -0.002,
    tp: float = 0.0,
    sl: float = 0.0,
) -> Event:
    signal = TradeSignal(
        strategy_id="funding_capture",
        symbol=symbol,
        side=side,
        entry_price=0.0,
        sl=sl,
        tp=tp,
        confidence=0.8,
        metadata={
            "type": sig_type,
            "funding_rate": funding_rate,
            "funding_rate_bps": abs(funding_rate) * 10_000,
            "next_funding_time": 1700000000000,
            "leverage": leverage,
            "exit_after_seconds": 30,
            "target_notional": target_notional,
            "last_price": last_price,
        },
        timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    return Event(type=EventType.SIGNAL_GENERATED, data=signal, source="test")


# ---------------------------------------------------------------------------
# Funding capture tests
# ---------------------------------------------------------------------------

class TestFundingCaptureExecution:
    """Full flow: funding signal -> order -> position tracked."""

    async def test_long_signal_places_buy_order(self, executor, mock_exchange, position_tracker):
        event = _make_signal(side=Side.LONG)
        await executor._on_signal(event)

        # First call is limit entry (PostOnly), may also poll fetch_order
        calls = mock_exchange.client.create_order.call_args_list
        assert len(calls) >= 1
        first_call = calls[0]
        # Positional args: (symbol, type, side, qty, ...)
        assert first_call[0][0] == "BTC/USDT:USDT"
        assert first_call[0][1] == "limit"
        assert first_call[0][2] == "buy"

    async def test_short_signal_places_sell_order(self, executor, mock_exchange):
        event = _make_signal(side=Side.SHORT)
        await executor._on_signal(event)

        first_call = mock_exchange.client.create_order.call_args_list[0]
        assert first_call[0][2] == "sell"

    async def test_leverage_set_correctly(self, executor, mock_exchange):
        event = _make_signal(leverage=5)
        await executor._on_signal(event)

        mock_exchange.set_leverage.assert_awaited_once_with(5, "BTC/USDT:USDT")

    async def test_position_tracked_after_entry(self, executor, position_tracker):
        event = _make_signal()
        await executor._on_signal(event)

        assert position_tracker.has_position("BTC/USDT:USDT")
        pos = position_tracker.get("BTC/USDT:USDT")
        assert pos.side == "long"
        assert pos.leverage == 10
        assert pos.strategy_id == "funding_capture"

    async def test_position_entry_price_from_order_response(self, executor, position_tracker):
        event = _make_signal()
        await executor._on_signal(event)

        pos = position_tracker.get("BTC/USDT:USDT")
        assert pos.entry_price == Decimal("50000.0")

    async def test_exit_task_scheduled(self, executor):
        event = _make_signal()
        await executor._on_signal(event)

        assert "BTC/USDT:USDT" in executor._pending_exits
        # Clean up the scheduled task
        executor._pending_exits["BTC/USDT:USDT"].cancel()


class TestDuplicateSignalPrevention:
    """Second signal for same symbol should be ignored."""

    async def test_duplicate_signal_ignored_when_position_open(
        self, executor, mock_exchange, position_tracker
    ):
        event = _make_signal()
        await executor._on_signal(event)
        assert mock_exchange.client.create_order.call_count == 1

        # Second signal for same symbol
        event2 = _make_signal()
        await executor._on_signal(event2)
        # Should still be 1 -- duplicate ignored
        assert mock_exchange.client.create_order.call_count == 1

    async def test_different_symbol_accepted(self, executor, mock_exchange):
        event1 = _make_signal(symbol="BTC/USDT:USDT")
        event2 = _make_signal(symbol="ETH/USDT:USDT")

        await executor._on_signal(event1)
        await executor._on_signal(event2)

        assert mock_exchange.client.create_order.call_count == 2


class TestOrderFailure:
    """When order placement fails, no position should be opened."""

    async def test_order_failure_no_position(self, executor, mock_exchange, position_tracker):
        mock_exchange.client.create_order.side_effect = Exception("exchange error")

        event = _make_signal()
        # Should not raise -- exception is caught internally
        await executor._on_signal(event)

        assert not position_tracker.has_position("BTC/USDT:USDT")
        assert position_tracker.count == 0


class TestEventDrivenExecution:
    """Event-driven (non-funding) signal -> entry + TP/SL orders."""

    async def test_event_driven_places_tp_sl(self, executor, mock_exchange):
        # Mock create_order on the adapter (event-driven uses adapter, not raw client)
        order_result = MagicMock()
        order_result.average = Decimal("50000")
        order_result.price = Decimal("50000")
        mock_exchange.create_order.return_value = order_result

        signal = TradeSignal(
            strategy_id="miro",
            symbol="BTC/USDT:USDT",
            side=Side.LONG,
            entry_price=50000.0,
            sl=49000.0,
            tp=52000.0,
            confidence=0.7,
            metadata={"type": "breakout"},
            timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
        )
        event = Event(type=EventType.SIGNAL_GENERATED, data=signal, source="test")
        await executor._on_signal(event)

        # Entry + TP + SL = 3 calls
        assert mock_exchange.create_order.call_count == 3

    async def test_event_driven_tracks_position(self, executor, mock_exchange, position_tracker):
        order_result = MagicMock()
        order_result.average = Decimal("3000")
        order_result.price = Decimal("3000")
        mock_exchange.create_order.return_value = order_result

        signal = TradeSignal(
            strategy_id="miro",
            symbol="ETH/USDT:USDT",
            side=Side.SHORT,
            entry_price=3000.0,
            sl=3100.0,
            tp=2800.0,
            confidence=0.6,
            metadata={"type": "breakout"},
            timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
        )
        event = Event(type=EventType.SIGNAL_GENERATED, data=signal, source="test")
        await executor._on_signal(event)

        assert position_tracker.has_position("ETH/USDT:USDT")
        pos = position_tracker.get("ETH/USDT:USDT")
        assert pos.side == "short"
        assert pos.strategy_id == "miro"


class TestPositionOpenedEvent:
    """Verify POSITION_OPENED event is published after successful entry."""

    async def test_position_opened_event_published(self, executor, event_bus):
        published = []
        event_bus.subscribe(
            EventType.POSITION_OPENED,
            lambda e: published.append(e) or asyncio.sleep(0),
        )

        event = _make_signal()
        await executor._on_signal(event)

        # The event is published via the queue, so we need to process it
        # Since we called _on_signal directly, the publish goes to queue
        # Check queue has the event
        assert not event_bus._queue.empty()
