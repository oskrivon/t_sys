"""Portfolio Manager — orchestrates N strategies.

Responsibilities:
  - Register and schedule strategies
  - Allocate capital per strategy
  - Resolve conflicts (same symbol, opposite sides)
  - Aggregate risk across strategies
  - Route signals to execution layer
  - Track per-strategy and aggregate P&L
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.strategies.base import (
    Strategy, StrategyConfig, StrategyType,
    TradeSignal, TargetPosition, Side,
)

log = structlog.get_logger()


@dataclass
class PortfolioState:
    """Aggregate portfolio state."""
    total_capital: float = 10_000.0
    strategies: dict[str, StrategyState] = field(default_factory=dict)

    def get_strategy_capital(self, strategy_id: str) -> float:
        state = self.strategies.get(strategy_id)
        if not state:
            return 0.0
        return self.total_capital * state.allocation_pct / 100

    def get_total_exposure(self) -> float:
        """Total absolute exposure across all strategies."""
        return sum(s.current_exposure for s in self.strategies.values())


@dataclass
class StrategyState:
    """Per-strategy tracking."""
    strategy_id: str
    allocation_pct: float = 50.0
    current_exposure: float = 0.0
    open_positions: int = 0
    total_pnl: float = 0.0
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    last_signal_time: Optional[datetime] = None


class PortfolioManager:
    """Manages N strategies with unified risk management."""

    def __init__(self, total_capital: float = 10_000.0, max_total_exposure_pct: float = 200.0):
        self.strategies: dict[str, Strategy] = {}
        self.state = PortfolioState(total_capital=total_capital)
        self.max_total_exposure_pct = max_total_exposure_pct
        self._conflict_positions: dict[str, list[tuple[str, Side]]] = {}  # symbol -> [(strategy_id, side)]

    def register_strategy(self, strategy: Strategy) -> None:
        """Register a strategy. Must be done before start."""
        sid = strategy.strategy_id
        self.strategies[sid] = strategy
        self.state.strategies[sid] = StrategyState(
            strategy_id=sid,
            allocation_pct=strategy.config.allocation_pct,
        )
        log.info("strategy_registered", strategy_id=sid,
                 name=strategy.config.name,
                 allocation=strategy.config.allocation_pct)

    async def initialize_all(self, exchange) -> None:
        """Initialize all registered strategies."""
        for sid, strategy in self.strategies.items():
            if strategy.config.enabled:
                await strategy.initialize(exchange)
                log.info("strategy_initialized", strategy_id=sid)

    async def run_strategy(self, strategy_id: str, exchange) -> list[TradeSignal | TargetPosition]:
        """Run one strategy tick and process results."""
        strategy = self.strategies.get(strategy_id)
        if not strategy or not strategy.config.enabled:
            return []

        results = await strategy.on_tick(exchange)
        if not results:
            return []

        # Update state
        ss = self.state.strategies[strategy_id]
        ss.last_signal_time = datetime.now(timezone.utc)

        # Risk checks
        filtered = []
        for item in results:
            if self._passes_risk_checks(strategy, item):
                filtered.append(item)

        if filtered:
            log.info("strategy_signals",
                     strategy_id=strategy_id,
                     count=len(filtered),
                     type=strategy.strategy_type.value)

        return filtered

    def _passes_risk_checks(self, strategy: Strategy, item: TradeSignal | TargetPosition) -> bool:
        """Check if a signal/position passes portfolio-level risk."""
        ss = self.state.strategies[strategy.strategy_id]

        # Max positions per strategy
        if isinstance(item, TradeSignal):
            if ss.open_positions >= strategy.config.max_positions:
                log.debug("risk_max_positions", strategy_id=strategy.strategy_id,
                          positions=ss.open_positions)
                return False

        # Max total exposure
        total_exp_pct = self.state.get_total_exposure() / self.state.total_capital * 100
        if total_exp_pct >= self.max_total_exposure_pct:
            log.debug("risk_max_exposure", exposure_pct=total_exp_pct)
            return False

        # Conflict check: same symbol opposite sides from different strategies
        if isinstance(item, TradeSignal):
            symbol = item.symbol
            if symbol in self._conflict_positions:
                for other_sid, other_side in self._conflict_positions[symbol]:
                    if other_sid != strategy.strategy_id and other_side != item.side:
                        log.info("risk_conflict",
                                 symbol=symbol,
                                 strategy_a=strategy.strategy_id,
                                 side_a=item.side.value,
                                 strategy_b=other_sid,
                                 side_b=other_side.value)
                        return False  # skip conflicting signal

        return True

    def record_trade_open(self, strategy_id: str, symbol: str, side: Side, size_usd: float) -> None:
        """Record that a trade was opened."""
        ss = self.state.strategies.get(strategy_id)
        if ss:
            ss.open_positions += 1
            ss.current_exposure += size_usd
            ss.trades_count += 1

        # Track for conflict detection
        if symbol not in self._conflict_positions:
            self._conflict_positions[symbol] = []
        self._conflict_positions[symbol].append((strategy_id, side))

    def record_trade_close(self, strategy_id: str, symbol: str, pnl_pct: float) -> None:
        """Record that a trade was closed."""
        ss = self.state.strategies.get(strategy_id)
        if ss:
            ss.open_positions = max(0, ss.open_positions - 1)
            ss.total_pnl += pnl_pct
            if pnl_pct > 0:
                ss.wins += 1
            else:
                ss.losses += 1

        # Remove from conflict tracking
        if symbol in self._conflict_positions:
            self._conflict_positions[symbol] = [
                (sid, s) for sid, s in self._conflict_positions[symbol]
                if sid != strategy_id
            ]
            if not self._conflict_positions[symbol]:
                del self._conflict_positions[symbol]

        # Notify strategy
        strategy = self.strategies.get(strategy_id)
        if strategy:
            import asyncio
            asyncio.create_task(strategy.on_trade_closed(symbol, pnl_pct))

    def get_allocation(self, strategy_id: str) -> float:
        """Get current capital allocated to a strategy."""
        return self.state.get_strategy_capital(strategy_id)

    def get_status(self) -> dict:
        """Full portfolio status."""
        strategies = {}
        for sid, ss in self.state.strategies.items():
            strategy = self.strategies.get(sid)
            wr = ss.wins / (ss.wins + ss.losses) if (ss.wins + ss.losses) > 0 else 0
            base_status = strategy.get_status() if strategy else {}
            strategies[sid] = {
                **base_status,
                "allocation_pct": ss.allocation_pct,
                "capital": self.state.get_strategy_capital(sid),
                "open_positions": ss.open_positions,
                "total_pnl_pct": ss.total_pnl,
                "trades": ss.trades_count,
                "win_rate": wr,
            }

        total_pnl = sum(ss.total_pnl for ss in self.state.strategies.values())
        total_trades = sum(ss.trades_count for ss in self.state.strategies.values())

        return {
            "total_capital": self.state.total_capital,
            "total_pnl_pct": total_pnl,
            "total_trades": total_trades,
            "total_exposure": self.state.get_total_exposure(),
            "strategies": strategies,
        }

    def rebalance_allocations(self, new_allocations: dict[str, float]) -> None:
        """Update strategy allocations. Values should sum to ~100."""
        total = sum(new_allocations.values())
        for sid, pct in new_allocations.items():
            if sid in self.state.strategies:
                self.state.strategies[sid].allocation_pct = pct
        log.info("allocations_rebalanced", allocations=new_allocations, total=total)
