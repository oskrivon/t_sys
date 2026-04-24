"""Integration tests: FundingCaptureStrategy end-to-end flow.

Tests the strategy lifecycle: initialization, WS event handling,
scheduling logic, and signal emission -- with mocked exchange and event bus.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.engine.event_bus import EventBus, Event, EventType
from src.strategies.base import Side, StrategyConfig, StrategyType
from src.strategies.funding_capture import FundingCaptureStrategy, FundingOpportunity


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def collecting_bus():
    """EventBus that collects published events for assertion."""
    bus = EventBus()
    bus._collected: list[Event] = []

    original_publish = bus.publish

    async def _collect_and_publish(event: Event):
        bus._collected.append(event)
        await original_publish(event)

    bus.publish = _collect_and_publish
    return bus


@pytest.fixture
def mock_raw_exchange():
    """Mock raw ccxt exchange for precompute."""
    from unittest.mock import MagicMock
    exchange = AsyncMock()
    exchange.set_leverage = AsyncMock()
    exchange.market = MagicMock(return_value={
        "limits": {"amount": {"min": 0.001}},
        "precision": {"amount": 0.001},
    })
    # Orderbook with sufficient depth for book depth check
    exchange.fetch_order_book = AsyncMock(return_value={
        "bids": [[50000.0, 1.0], [49999.0, 2.0]],
        "asks": [[50001.0, 1.0], [50002.0, 2.0]],
    })
    return exchange


@pytest.fixture
def strategy(funding_config, collecting_bus, mock_raw_exchange):
    s = FundingCaptureStrategy(funding_config, collecting_bus)
    s._exchange_ref = mock_raw_exchange
    return s


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _make_funding_event(
    symbol_raw: str = "BTCUSDT",
    funding_rate: float = -0.002,
    next_funding_time_ms: int | None = None,
    last_price: float = 50000.0,
) -> Event:
    """Create a FUNDING_RATE event as the WS handler would receive."""
    if next_funding_time_ms is None:
        # Default: 60s from now (within the 120s window)
        next_funding_time_ms = _now_ms() + 60_000

    return Event(
        type=EventType.FUNDING_RATE,
        data={
            "_symbol_raw": symbol_raw,
            "fundingRate": str(funding_rate),
            "nextFundingTime": str(next_funding_time_ms),
            "lastPrice": str(last_price),
        },
        source="ws",
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInitialization:
    async def test_initialize_starts_scan_task(self, strategy, collecting_bus):
        mock_exchange = AsyncMock()

        # Patch _scan_loop to avoid real scanning
        with patch.object(strategy, "_scan_loop", new_callable=AsyncMock) as mock_scan:
            await strategy.initialize(mock_exchange)

            assert strategy._scan_task is not None
            assert strategy._exchange_ref is mock_exchange

            # Cancel the background task to avoid warnings
            strategy._scan_task.cancel()
            try:
                await strategy._scan_task
            except asyncio.CancelledError:
                pass

    async def test_initialize_subscribes_to_funding_events(self, strategy, collecting_bus):
        mock_exchange = AsyncMock()
        with patch.object(strategy, "_scan_loop", new_callable=AsyncMock):
            await strategy.initialize(mock_exchange)

            # Verify subscription exists
            handlers = collecting_bus._subscribers.get(EventType.FUNDING_RATE, [])
            assert any(h == strategy._on_funding_update for h in handlers)

            strategy._scan_task.cancel()
            try:
                await strategy._scan_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Funding update handling
# ---------------------------------------------------------------------------

class TestFundingUpdateHandler:
    """Test _on_funding_update with various rate/timing combinations."""

    async def test_high_rate_near_settlement_schedules_entry(self, strategy):
        # Rate = 20bps (above 15bps threshold), 60s to funding (within 120s)
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.002,  # 20bps
            next_funding_time_ms=_now_ms() + 60_000,
        )
        await strategy._on_funding_update(event)

        assert "BTCUSDT" in strategy._scheduled
        assert "BTCUSDT" in strategy._traded_this_round

        # Cleanup
        strategy._scheduled["BTCUSDT"].cancel()

    async def test_low_rate_does_not_schedule(self, strategy):
        # Rate = 5bps (below 15bps threshold)
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.0005,  # 5bps
            next_funding_time_ms=_now_ms() + 60_000,
        )
        await strategy._on_funding_update(event)

        assert "BTCUSDT" not in strategy._scheduled
        assert "BTCUSDT" not in strategy._traded_this_round

    async def test_distant_funding_time_does_not_schedule(self, strategy):
        # Rate is high but funding is 5 min away (> 120s)
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.002,  # 20bps
            next_funding_time_ms=_now_ms() + 300_000,  # 5 min
        )
        await strategy._on_funding_update(event)

        assert "BTCUSDT" not in strategy._scheduled

    async def test_unmonitored_symbol_ignored(self, strategy):
        # Symbol not in _monitored set
        event = _make_funding_event(symbol_raw="OBSCURECOINUSDT")
        await strategy._on_funding_update(event)

        assert "OBSCURECOINUSDT" not in strategy._scheduled
        assert "OBSCURECOINUSDT" not in strategy._opportunities

    async def test_duplicate_not_scheduled_twice(self, strategy):
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.002,
            next_funding_time_ms=_now_ms() + 60_000,
        )
        await strategy._on_funding_update(event)
        first_task = strategy._scheduled.get("BTCUSDT")

        # Second event for same symbol
        await strategy._on_funding_update(event)
        second_task = strategy._scheduled.get("BTCUSDT")

        # Should be the same task object -- not re-scheduled
        assert first_task is second_task

        # Cleanup
        first_task.cancel()

    async def test_traded_this_round_prevents_reschedule(self, strategy):
        # Manually add to traded set (simulating already traded)
        strategy._traded_this_round.add("ETHUSDT")
        strategy._monitored.add("ETHUSDT")

        event = _make_funding_event(
            symbol_raw="ETHUSDT",
            funding_rate=0.003,  # 30bps
            next_funding_time_ms=_now_ms() + 60_000,
        )
        await strategy._on_funding_update(event)

        assert "ETHUSDT" not in strategy._scheduled

    async def test_traded_this_round_clears_when_far_from_settlement(self, strategy):
        strategy._traded_this_round.add("BTCUSDT")
        strategy._traded_this_round.add("ETHUSDT")

        # Funding is 15 min away (> 600s threshold for reset)
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.002,
            next_funding_time_ms=_now_ms() + 900_000,  # 15 min
        )
        await strategy._on_funding_update(event)

        # traded_this_round should be cleared
        assert len(strategy._traded_this_round) == 0

    async def test_opportunity_stored(self, strategy):
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.002,
            next_funding_time_ms=_now_ms() + 60_000,
            last_price=50000.0,
        )
        await strategy._on_funding_update(event)

        assert "BTCUSDT" in strategy._opportunities
        opp = strategy._opportunities["BTCUSDT"]
        assert opp.direction == Side.LONG  # negative rate -> LONG
        assert opp.last_price == 50000.0

        # Cleanup
        if "BTCUSDT" in strategy._scheduled:
            strategy._scheduled["BTCUSDT"].cancel()

    async def test_negative_rate_yields_long_direction(self, strategy):
        event = _make_funding_event(
            symbol_raw="BTCUSDT",
            funding_rate=-0.003,
            next_funding_time_ms=_now_ms() + 60_000,
        )
        await strategy._on_funding_update(event)

        opp = strategy._opportunities["BTCUSDT"]
        assert opp.direction == Side.LONG

        if "BTCUSDT" in strategy._scheduled:
            strategy._scheduled["BTCUSDT"].cancel()

    async def test_positive_rate_yields_short_direction(self, strategy):
        strategy._monitored.add("ETHUSDT")

        event = _make_funding_event(
            symbol_raw="ETHUSDT",
            funding_rate=0.003,
            next_funding_time_ms=_now_ms() + 60_000,
        )
        await strategy._on_funding_update(event)

        opp = strategy._opportunities["ETHUSDT"]
        assert opp.direction == Side.SHORT

        if "ETHUSDT" in strategy._scheduled:
            strategy._scheduled["ETHUSDT"].cancel()


# ---------------------------------------------------------------------------
# Signal emission
# ---------------------------------------------------------------------------

class TestSignalEmission:
    """Test that _schedule_entry emits a properly formed signal."""

    async def test_signal_emitted_with_correct_metadata(self, strategy, collecting_bus):
        # next_funding_time 1s in future so all sleeps are negative → skipped
        nft = _now_ms() + 1_000
        opp = FundingOpportunity(
            symbol_raw="BTCUSDT",
            symbol_ccxt="BTC/USDT:USDT",
            funding_rate=-0.002,
            next_funding_time=nft,
            direction=Side.LONG,
            last_price=50000.0,
        )
        strategy._opportunities["BTCUSDT"] = opp

        await strategy._schedule_entry(opp, secs_until=0)

        signal_events = [
            e for e in collecting_bus._collected
            if e.type == EventType.SIGNAL_GENERATED
        ]
        assert len(signal_events) == 1

        signal = signal_events[0].data
        assert signal.symbol == "BTC/USDT:USDT"
        assert signal.side == Side.LONG
        assert signal.metadata["type"] == "funding_capture"
        assert signal.metadata["funding_rate"] == -0.002
        assert signal.metadata["leverage"] == 10
        assert signal.strategy_id == "funding_capture"
        # Precompute must be wired: qty pre-computed, leverage pre-set
        assert "_precomputed_qty" in signal.metadata, "precomputed qty missing — order will use min_qty!"
        assert signal.metadata["_precomputed_qty"] > 0
        assert signal.metadata["_leverage_set"] is True
        assert signal.metadata["target_notional"] == 25.0

    async def test_signal_cancelled_when_rate_drops(self, strategy, collecting_bus):
        nft = _now_ms() + 1_000
        opp = FundingOpportunity(
            symbol_raw="BTCUSDT",
            symbol_ccxt="BTC/USDT:USDT",
            funding_rate=-0.002,  # 20bps at scheduling time
            next_funding_time=nft,
            direction=Side.LONG,
            last_price=50000.0,
        )
        # Update the stored opportunity to have low rate (simulating rate drop)
        low_opp = FundingOpportunity(
            symbol_raw="BTCUSDT",
            symbol_ccxt="BTC/USDT:USDT",
            funding_rate=-0.0001,  # 1bps -- below threshold
            next_funding_time=nft,
            direction=Side.LONG,
            last_price=50000.0,
        )
        strategy._opportunities["BTCUSDT"] = low_opp

        await strategy._schedule_entry(opp, secs_until=0)

        signal_events = [
            e for e in collecting_bus._collected
            if e.type == EventType.SIGNAL_GENERATED
        ]
        assert len(signal_events) == 0  # no signal emitted

    async def test_signal_cancelled_when_no_opportunity(self, strategy, collecting_bus):
        nft = _now_ms() + 1_000
        opp = FundingOpportunity(
            symbol_raw="BTCUSDT",
            symbol_ccxt="BTC/USDT:USDT",
            funding_rate=-0.002,
            next_funding_time=nft,
            direction=Side.LONG,
            last_price=50000.0,
        )
        # Don't store in _opportunities -- simulates it being removed
        await strategy._schedule_entry(opp, secs_until=0)

        signal_events = [
            e for e in collecting_bus._collected
            if e.type == EventType.SIGNAL_GENERATED
        ]
        assert len(signal_events) == 0


# ---------------------------------------------------------------------------
# Get opportunities
# ---------------------------------------------------------------------------

class TestGetOpportunities:
    async def test_returns_above_threshold_sorted(self, strategy):
        strategy._opportunities["BTCUSDT"] = FundingOpportunity(
            symbol_raw="BTCUSDT", symbol_ccxt="BTC/USDT:USDT",
            funding_rate=-0.003, next_funding_time=0,
            direction=Side.LONG, last_price=50000.0,
        )
        strategy._opportunities["ETHUSDT"] = FundingOpportunity(
            symbol_raw="ETHUSDT", symbol_ccxt="ETH/USDT:USDT",
            funding_rate=0.005, next_funding_time=0,
            direction=Side.SHORT, last_price=3000.0,
        )
        strategy._opportunities["LOWUSDT"] = FundingOpportunity(
            symbol_raw="LOWUSDT", symbol_ccxt="LOW/USDT:USDT",
            funding_rate=-0.0001, next_funding_time=0,  # 1bps, below threshold
            direction=Side.LONG, last_price=1.0,
        )

        opps = strategy.get_opportunities()
        assert len(opps) == 2  # LOWUSDT excluded
        assert opps[0]["symbol"] == "ETHUSDT"  # 50bps first
        assert opps[1]["symbol"] == "BTCUSDT"  # 30bps second
