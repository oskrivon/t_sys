"""Base strategy interface.

All strategies implement this ABC. Two modes:
  - Event-driven: produces signals (e.g., Miro S/R levels)
  - Systematic: produces target portfolio (e.g., Volume Ranking)

The PortfolioManager calls each strategy on its schedule and merges results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class StrategyType(str, Enum):
    EVENT_DRIVEN = "event_driven"    # signal → trade → TP/SL
    SYSTEMATIC = "systematic"        # target portfolio → rebalance


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class TradeSignal:
    """A single trade signal from an event-driven strategy."""
    strategy_id: str
    symbol: str
    side: Side
    entry_price: float
    sl: float
    tp: float
    confidence: float = 0.0          # 0-1, from ML/Vision
    metadata: dict = field(default_factory=dict)  # strategy-specific data
    timestamp: Optional[datetime] = None


@dataclass
class TargetPosition:
    """Target position for systematic strategies."""
    symbol: str
    side: Side
    weight: float       # fraction of strategy allocation (0-1)
    score: float = 0.0  # ranking score for prioritization


@dataclass
class StrategyConfig:
    """Configuration for a strategy instance."""
    strategy_id: str
    name: str
    strategy_type: StrategyType
    enabled: bool = True
    allocation_pct: float = 50.0      # % of total capital
    max_positions: int = 10           # max concurrent positions
    risk_per_trade_pct: float = 4.0   # % of strategy capital per trade
    schedule_cron: str = ""           # cron expression or "4h", "1d"
    params: dict = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base for all strategies."""

    def __init__(self, config: StrategyConfig):
        self.config = config

    @property
    def strategy_id(self) -> str:
        return self.config.strategy_id

    @property
    def strategy_type(self) -> StrategyType:
        return self.config.strategy_type

    @abstractmethod
    async def initialize(self, exchange) -> None:
        """Load models, warm up data, etc."""

    @abstractmethod
    async def on_tick(self, exchange) -> list[TradeSignal] | list[TargetPosition]:
        """Called on schedule. Returns signals or target positions.

        Event-driven strategies return TradeSignal list.
        Systematic strategies return TargetPosition list.
        """

    async def on_trade_closed(self, symbol: str, pnl_pct: float) -> None:
        """Callback when a trade from this strategy closes. Override if needed."""

    def get_status(self) -> dict:
        """Return strategy status for monitoring."""
        return {
            "strategy_id": self.strategy_id,
            "name": self.config.name,
            "type": self.strategy_type.value,
            "enabled": self.config.enabled,
        }
