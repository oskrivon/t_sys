"""Universal trade record and cost breakdown for backtesting."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class ExitReason(str, Enum):
    TP = "tp"
    SL = "sl"
    TIMEOUT = "timeout"
    SIGNAL = "signal"
    REBALANCE = "rebalance"
    LIQUIDATION = "liquidation"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Itemized costs for a single trade, all as fractions (not bps).

    Example: 5.5 bps taker fee per side = entry_fee=0.00055, exit_fee=0.00055.
    """
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    slippage: float = 0.0       # both sides combined
    spread: float = 0.0         # both sides combined
    funding: float = 0.0        # net funding paid during hold (negative = received)
    borrow: float = 0.0         # margin / short borrow cost

    @property
    def total(self) -> float:
        return (self.entry_fee + self.exit_fee + self.slippage
                + self.spread + self.funding + self.borrow)


@dataclass(slots=True)
class Trade:
    """Universal trade record produced by any strategy type."""
    symbol: str
    side: Side
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size_usd: float             # notional at entry
    exit_reason: ExitReason

    # P&L (auto-computed if left at 0)
    gross_pnl_pct: float = 0.0
    costs: CostBreakdown = field(default_factory=CostBreakdown)
    net_pnl_pct: float = 0.0
    net_pnl_usd: float = 0.0

    # Position details
    leverage: float = 1.0
    strategy_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.gross_pnl_pct == 0.0 and self.entry_price > 0:
            if self.side == Side.LONG:
                self.gross_pnl_pct = (self.exit_price - self.entry_price) / self.entry_price
            else:
                self.gross_pnl_pct = (self.entry_price - self.exit_price) / self.entry_price
        if self.net_pnl_pct == 0.0:
            self.net_pnl_pct = self.gross_pnl_pct - self.costs.total
        if self.net_pnl_usd == 0.0:
            self.net_pnl_usd = self.net_pnl_pct * self.size_usd

    def recompute(self) -> None:
        """Recompute net P&L after costs are updated externally."""
        self.net_pnl_pct = self.gross_pnl_pct - self.costs.total
        self.net_pnl_usd = self.net_pnl_pct * self.size_usd
