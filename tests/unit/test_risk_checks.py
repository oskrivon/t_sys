"""Tests for PortfolioManager._passes_risk_checks.

Validates max-position limits, total exposure cap,
and cross-strategy symbol conflict detection.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.portfolio.manager import PortfolioManager, StrategyState
from src.strategies.base import (
    Side, StrategyConfig, StrategyType, TradeSignal, TargetPosition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(
    strategy_id: str = "strat_a",
    max_positions: int = 10,
    allocation_pct: float = 50.0,
) -> MagicMock:
    """Create a minimal mock Strategy with the fields risk checks need."""
    config = StrategyConfig(
        strategy_id=strategy_id,
        name=f"Test {strategy_id}",
        strategy_type=StrategyType.EVENT_DRIVEN,
        max_positions=max_positions,
        allocation_pct=allocation_pct,
    )
    strategy = MagicMock()
    strategy.strategy_id = strategy_id
    strategy.config = config
    return strategy


def _make_signal(
    strategy_id: str = "strat_a",
    symbol: str = "BTC/USDT:USDT",
    side: Side = Side.LONG,
) -> TradeSignal:
    return TradeSignal(
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        entry_price=50_000.0,
        sl=49_000.0,
        tp=53_000.0,
    )


def _make_manager(total_capital: float = 10_000.0, max_exposure_pct: float = 200.0) -> PortfolioManager:
    return PortfolioManager(total_capital=total_capital, max_total_exposure_pct=max_exposure_pct)


# ---------------------------------------------------------------------------
# Max positions per strategy
# ---------------------------------------------------------------------------

class TestMaxPositions:
    """TradeSignal blocked when open_positions >= max_positions."""

    def test_under_max_positions_passes(self):
        pm = _make_manager()
        strat = _make_strategy(max_positions=5)
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a", open_positions=4)
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is True

    def test_at_max_positions_fails(self):
        pm = _make_manager()
        strat = _make_strategy(max_positions=5)
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a", open_positions=5)
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is False

    def test_over_max_positions_fails(self):
        pm = _make_manager()
        strat = _make_strategy(max_positions=5)
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a", open_positions=6)
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is False

    def test_target_position_skips_max_position_check(self):
        """TargetPosition is not a TradeSignal — max_positions check is skipped."""
        pm = _make_manager()
        strat = _make_strategy(max_positions=1)
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a", open_positions=10)
        target = TargetPosition(symbol="BTC/USDT:USDT", side=Side.LONG, weight=0.5)
        assert pm._passes_risk_checks(strat, target) is True


# ---------------------------------------------------------------------------
# Max total exposure
# ---------------------------------------------------------------------------

class TestMaxExposure:
    """Signal blocked when total_exposure / total_capital * 100 >= threshold."""

    def test_under_max_exposure_passes(self):
        pm = _make_manager(total_capital=10_000.0, max_exposure_pct=200.0)
        strat = _make_strategy()
        pm.state.strategies["strat_a"] = StrategyState(
            strategy_id="strat_a", current_exposure=19_000.0,
        )
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is True

    def test_at_max_exposure_fails(self):
        pm = _make_manager(total_capital=10_000.0, max_exposure_pct=200.0)
        strat = _make_strategy()
        pm.state.strategies["strat_a"] = StrategyState(
            strategy_id="strat_a", current_exposure=20_000.0,
        )
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is False

    def test_over_max_exposure_fails(self):
        pm = _make_manager(total_capital=10_000.0, max_exposure_pct=200.0)
        strat = _make_strategy()
        pm.state.strategies["strat_a"] = StrategyState(
            strategy_id="strat_a", current_exposure=25_000.0,
        )
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is False

    def test_exposure_aggregated_across_strategies(self):
        """Total exposure sums across all strategies."""
        pm = _make_manager(total_capital=10_000.0, max_exposure_pct=200.0)
        strat = _make_strategy(strategy_id="strat_a")
        pm.state.strategies["strat_a"] = StrategyState(
            strategy_id="strat_a", current_exposure=10_000.0,
        )
        pm.state.strategies["strat_b"] = StrategyState(
            strategy_id="strat_b", current_exposure=10_000.0,
        )
        # Total = 20000 / 10000 * 100 = 200% → at limit → fails
        signal = _make_signal()
        assert pm._passes_risk_checks(strat, signal) is False


# ---------------------------------------------------------------------------
# Symbol conflict detection
# ---------------------------------------------------------------------------

class TestSymbolConflict:
    """Same symbol, opposite side, different strategy → blocked."""

    def test_no_conflicts_passes(self):
        pm = _make_manager()
        strat = _make_strategy()
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a")
        signal = _make_signal(symbol="BTC/USDT:USDT", side=Side.LONG)
        # No entries in _conflict_positions
        assert pm._passes_risk_checks(strat, signal) is True

    def test_conflict_opposite_side_different_strategy_fails(self):
        """strat_b is SHORT BTC, strat_a tries LONG BTC → conflict."""
        pm = _make_manager()
        strat_a = _make_strategy(strategy_id="strat_a")
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a")
        pm._conflict_positions["BTC/USDT:USDT"] = [("strat_b", Side.SHORT)]
        signal = _make_signal(strategy_id="strat_a", symbol="BTC/USDT:USDT", side=Side.LONG)
        assert pm._passes_risk_checks(strat_a, signal) is False

    def test_same_side_different_strategy_passes(self):
        """strat_b is LONG BTC, strat_a also LONG BTC → no conflict."""
        pm = _make_manager()
        strat_a = _make_strategy(strategy_id="strat_a")
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a")
        pm._conflict_positions["BTC/USDT:USDT"] = [("strat_b", Side.LONG)]
        signal = _make_signal(strategy_id="strat_a", symbol="BTC/USDT:USDT", side=Side.LONG)
        assert pm._passes_risk_checks(strat_a, signal) is True

    def test_opposite_side_same_strategy_passes(self):
        """strat_a is SHORT BTC, strat_a tries LONG BTC → same strategy, no conflict."""
        pm = _make_manager()
        strat_a = _make_strategy(strategy_id="strat_a")
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a")
        pm._conflict_positions["BTC/USDT:USDT"] = [("strat_a", Side.SHORT)]
        signal = _make_signal(strategy_id="strat_a", symbol="BTC/USDT:USDT", side=Side.LONG)
        assert pm._passes_risk_checks(strat_a, signal) is True

    def test_conflict_on_different_symbol_passes(self):
        """Conflict exists on ETH, signal is for BTC → no conflict."""
        pm = _make_manager()
        strat_a = _make_strategy(strategy_id="strat_a")
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a")
        pm._conflict_positions["ETH/USDT:USDT"] = [("strat_b", Side.SHORT)]
        signal = _make_signal(strategy_id="strat_a", symbol="BTC/USDT:USDT", side=Side.LONG)
        assert pm._passes_risk_checks(strat_a, signal) is True

    def test_target_position_skips_conflict_check(self):
        """TargetPosition is not a TradeSignal — conflict check is skipped."""
        pm = _make_manager()
        strat_a = _make_strategy(strategy_id="strat_a")
        pm.state.strategies["strat_a"] = StrategyState(strategy_id="strat_a")
        pm._conflict_positions["BTC/USDT:USDT"] = [("strat_b", Side.SHORT)]
        target = TargetPosition(symbol="BTC/USDT:USDT", side=Side.LONG, weight=0.5)
        assert pm._passes_risk_checks(strat_a, target) is True
