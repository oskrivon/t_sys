"""Integration tests: PortfolioManager with multiple strategies.

Tests strategy registration, capital allocation, risk checks,
trade recording, and conflict detection.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.portfolio.manager import PortfolioManager, StrategyState
from src.strategies.base import (
    Strategy, StrategyConfig, StrategyType, TradeSignal, TargetPosition, Side,
)


# ---------------------------------------------------------------------------
# Dummy strategy for testing (concrete implementation of ABC)
# ---------------------------------------------------------------------------

class DummyStrategy(Strategy):
    """Minimal Strategy implementation for testing."""

    def __init__(self, config: StrategyConfig):
        super().__init__(config)
        self._signals: list[TradeSignal] = []

    async def initialize(self, exchange) -> None:
        pass

    async def on_tick(self, exchange) -> list[TradeSignal]:
        return self._signals

    async def on_trade_closed(self, symbol: str, pnl_pct: float) -> None:
        pass

    def set_next_signals(self, signals: list[TradeSignal]) -> None:
        self._signals = signals


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pm():
    return PortfolioManager(total_capital=10_000.0, max_total_exposure_pct=200.0)


@pytest.fixture
def funding_strategy():
    config = StrategyConfig(
        strategy_id="funding_capture",
        name="Funding Capture",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=30.0,
        max_positions=20,
    )
    return DummyStrategy(config)


@pytest.fixture
def miro_strategy():
    config = StrategyConfig(
        strategy_id="miro_breakout",
        name="Miro Breakout",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=50.0,
        max_positions=5,
    )
    return DummyStrategy(config)


@pytest.fixture
def volume_strategy():
    config = StrategyConfig(
        strategy_id="volume_ranking",
        name="Volume Ranking L/S",
        strategy_type=StrategyType.SYSTEMATIC,
        allocation_pct=20.0,
        max_positions=10,
    )
    return DummyStrategy(config)


def _make_signal(
    strategy_id: str = "miro_breakout",
    symbol: str = "BTC/USDT:USDT",
    side: Side = Side.LONG,
) -> TradeSignal:
    return TradeSignal(
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        entry_price=50000.0,
        sl=49000.0,
        tp=52000.0,
        confidence=0.7,
        timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_single_strategy(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)

        assert "funding_capture" in pm.strategies
        assert "funding_capture" in pm.state.strategies

    def test_register_multiple_strategies(self, pm, funding_strategy, miro_strategy, volume_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)
        pm.register_strategy(volume_strategy)

        assert len(pm.strategies) == 3
        assert len(pm.state.strategies) == 3

    def test_strategy_state_initialized(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)

        state = pm.state.strategies["funding_capture"]
        assert state.allocation_pct == 30.0
        assert state.open_positions == 0
        assert state.total_pnl == 0.0
        assert state.trades_count == 0


# ---------------------------------------------------------------------------
# Capital allocation
# ---------------------------------------------------------------------------

class TestCapitalAllocation:
    def test_allocation_respects_percentages(self, pm, funding_strategy, miro_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)

        assert pm.get_allocation("funding_capture") == 3000.0  # 30% of 10k
        assert pm.get_allocation("miro_breakout") == 5000.0    # 50% of 10k

    def test_allocation_unknown_strategy_returns_zero(self, pm):
        assert pm.get_allocation("nonexistent") == 0.0

    def test_rebalance_updates_allocations(self, pm, funding_strategy, miro_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)

        pm.rebalance_allocations({
            "funding_capture": 60.0,
            "miro_breakout": 40.0,
        })

        assert pm.get_allocation("funding_capture") == 6000.0
        assert pm.get_allocation("miro_breakout") == 4000.0


# ---------------------------------------------------------------------------
# Risk checks
# ---------------------------------------------------------------------------

class TestRiskChecks:
    async def test_max_positions_blocks_signal(self, pm, miro_strategy):
        pm.register_strategy(miro_strategy)

        # Fill up positions to max (5)
        for i in range(5):
            pm.record_trade_open(
                "miro_breakout", f"COIN{i}/USDT:USDT", Side.LONG, 100.0
            )

        # Next signal should be blocked
        signal = _make_signal(symbol="EXTRA/USDT:USDT")
        miro_strategy.set_next_signals([signal])

        results = await pm.run_strategy("miro_breakout", AsyncMock())
        assert len(results) == 0

    async def test_under_max_positions_allows_signal(self, pm, miro_strategy):
        pm.register_strategy(miro_strategy)

        # Only 2 positions (max is 5)
        pm.record_trade_open("miro_breakout", "BTC/USDT:USDT", Side.LONG, 100.0)
        pm.record_trade_open("miro_breakout", "ETH/USDT:USDT", Side.LONG, 100.0)

        signal = _make_signal(symbol="SOL/USDT:USDT")
        miro_strategy.set_next_signals([signal])

        results = await pm.run_strategy("miro_breakout", AsyncMock())
        assert len(results) == 1

    async def test_max_exposure_blocks_signal(self, pm, miro_strategy):
        pm.register_strategy(miro_strategy)

        # Set exposure to 200% of capital (max_total_exposure_pct=200)
        pm.record_trade_open("miro_breakout", "BTC/USDT:USDT", Side.LONG, 20_000.0)

        signal = _make_signal(symbol="ETH/USDT:USDT")
        miro_strategy.set_next_signals([signal])

        results = await pm.run_strategy("miro_breakout", AsyncMock())
        assert len(results) == 0

    async def test_conflict_blocks_opposite_side(self, pm, funding_strategy, miro_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)

        # Funding has a LONG on BTC
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 100.0)

        # Miro tries to SHORT BTC -- conflict
        signal = _make_signal(
            strategy_id="miro_breakout",
            symbol="BTC/USDT:USDT",
            side=Side.SHORT,
        )
        miro_strategy.set_next_signals([signal])

        results = await pm.run_strategy("miro_breakout", AsyncMock())
        assert len(results) == 0

    async def test_same_side_no_conflict(self, pm, funding_strategy, miro_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)

        # Both LONG on BTC -- no conflict
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 100.0)

        signal = _make_signal(
            strategy_id="miro_breakout",
            symbol="BTC/USDT:USDT",
            side=Side.LONG,
        )
        miro_strategy.set_next_signals([signal])

        results = await pm.run_strategy("miro_breakout", AsyncMock())
        assert len(results) == 1

    async def test_disabled_strategy_returns_empty(self, pm, miro_strategy):
        miro_strategy.config.enabled = False
        pm.register_strategy(miro_strategy)

        signal = _make_signal()
        miro_strategy.set_next_signals([signal])

        results = await pm.run_strategy("miro_breakout", AsyncMock())
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Trade recording
# ---------------------------------------------------------------------------

class TestTradeRecording:
    def test_record_trade_open(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)

        state = pm.state.strategies["funding_capture"]
        assert state.open_positions == 1
        assert state.current_exposure == 500.0
        assert state.trades_count == 1

    def test_record_multiple_opens(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)
        pm.record_trade_open("funding_capture", "ETH/USDT:USDT", Side.SHORT, 300.0)

        state = pm.state.strategies["funding_capture"]
        assert state.open_positions == 2
        assert state.current_exposure == 800.0
        assert state.trades_count == 2

    async def test_record_trade_close_win(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)
        pm.record_trade_close("funding_capture", "BTC/USDT:USDT", pnl_pct=5.0)

        state = pm.state.strategies["funding_capture"]
        assert state.open_positions == 0
        assert state.total_pnl == 5.0
        assert state.wins == 1
        assert state.losses == 0

    async def test_record_trade_close_loss(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)
        pm.record_trade_close("funding_capture", "BTC/USDT:USDT", pnl_pct=-3.0)

        state = pm.state.strategies["funding_capture"]
        assert state.losses == 1
        assert state.wins == 0
        assert state.total_pnl == -3.0

    async def test_close_removes_from_conflicts(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)

        assert "BTC/USDT:USDT" in pm._conflict_positions

        pm.record_trade_close("funding_capture", "BTC/USDT:USDT", pnl_pct=1.0)

        assert "BTC/USDT:USDT" not in pm._conflict_positions

    async def test_open_positions_never_negative(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        # Close without prior open
        pm.record_trade_close("funding_capture", "BTC/USDT:USDT", pnl_pct=-1.0)

        state = pm.state.strategies["funding_capture"]
        assert state.open_positions == 0  # clamped to 0


# ---------------------------------------------------------------------------
# Portfolio status
# ---------------------------------------------------------------------------

class TestPortfolioStatus:
    async def test_status_aggregates_across_strategies(self, pm, funding_strategy, miro_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)

        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)
        pm.record_trade_close("funding_capture", "BTC/USDT:USDT", pnl_pct=3.0)

        pm.record_trade_open("miro_breakout", "ETH/USDT:USDT", Side.SHORT, 300.0)
        pm.record_trade_close("miro_breakout", "ETH/USDT:USDT", pnl_pct=-1.0)

        status = pm.get_status()
        assert status["total_capital"] == 10_000.0
        assert status["total_pnl_pct"] == 2.0  # 3.0 + (-1.0)
        assert status["total_trades"] == 2
        assert "funding_capture" in status["strategies"]
        assert "miro_breakout" in status["strategies"]

    async def test_win_rate_calculated(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)

        pm.record_trade_open("funding_capture", "A/USDT:USDT", Side.LONG, 100.0)
        pm.record_trade_close("funding_capture", "A/USDT:USDT", pnl_pct=5.0)

        pm.record_trade_open("funding_capture", "B/USDT:USDT", Side.LONG, 100.0)
        pm.record_trade_close("funding_capture", "B/USDT:USDT", pnl_pct=-2.0)

        pm.record_trade_open("funding_capture", "C/USDT:USDT", Side.LONG, 100.0)
        pm.record_trade_close("funding_capture", "C/USDT:USDT", pnl_pct=3.0)

        status = pm.get_status()
        wr = status["strategies"]["funding_capture"]["win_rate"]
        assert abs(wr - 2 / 3) < 0.01  # 2 wins / 3 total


# ---------------------------------------------------------------------------
# Total exposure tracking
# ---------------------------------------------------------------------------

class TestExposureTracking:
    def test_total_exposure_across_strategies(self, pm, funding_strategy, miro_strategy):
        pm.register_strategy(funding_strategy)
        pm.register_strategy(miro_strategy)

        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 500.0)
        pm.record_trade_open("miro_breakout", "ETH/USDT:USDT", Side.SHORT, 300.0)

        assert pm.state.get_total_exposure() == 800.0

    def test_exposure_in_status(self, pm, funding_strategy):
        pm.register_strategy(funding_strategy)
        pm.record_trade_open("funding_capture", "BTC/USDT:USDT", Side.LONG, 1000.0)

        status = pm.get_status()
        assert status["total_exposure"] == 1000.0
