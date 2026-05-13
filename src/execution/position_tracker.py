"""Track live positions and their lifecycle."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


@dataclass
class LivePosition:
    """A live position on the exchange."""
    symbol: str
    side: str                       # "long" or "short"
    qty: Decimal                    # contracts
    entry_price: Decimal
    leverage: int = 1
    strategy_id: str = ""
    unrealized_pnl: Decimal = Decimal("0")
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # For funding capture: scheduled exit time
    exit_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
    # TP/SL order IDs for reconciliation
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_price: Optional[Decimal] = None
    sl_price: Optional[Decimal] = None

    @property
    def notional(self) -> Decimal:
        return self.qty * self.entry_price

    @property
    def margin(self) -> Decimal:
        return self.notional / self.leverage if self.leverage else self.notional


class PositionTracker:
    """In-memory position tracking. Sync with exchange on startup and periodically."""

    def __init__(self) -> None:
        self._positions: dict[str, LivePosition] = {}  # symbol -> position

    def open(self, pos: LivePosition) -> None:
        self._positions[pos.symbol] = pos

    def close(self, symbol: str) -> Optional[LivePosition]:
        return self._positions.pop(symbol, None)

    def get(self, symbol: str) -> Optional[LivePosition]:
        return self._positions.get(symbol)

    def get_all(self) -> list[LivePosition]:
        return list(self._positions.values())

    def get_by_strategy(self, strategy_id: str) -> list[LivePosition]:
        return [p for p in self._positions.values() if p.strategy_id == strategy_id]

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    @property
    def count(self) -> int:
        return len(self._positions)

    def total_exposure(self) -> Decimal:
        return sum((p.notional for p in self._positions.values()), Decimal("0"))

    def sync_from_exchange(self, exchange_positions: list[dict]) -> None:
        """Sync tracker state with exchange positions (from fetch_positions)."""
        exchange_syms = set()
        for p in exchange_positions:
            contracts = float(p.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            sym = p["symbol"]
            exchange_syms.add(sym)
            if sym not in self._positions:
                # Position exists on exchange but not tracked — adopt it
                self._positions[sym] = LivePosition(
                    symbol=sym,
                    side=p.get("side", "long"),
                    qty=Decimal(str(contracts)),
                    entry_price=Decimal(str(p.get("entryPrice", 0))),
                    leverage=int(p.get("leverage", 1) or 1),
                    strategy_id="unknown",
                    metadata={"synced_from_exchange": True},
                )
            else:
                # Update tracked position with exchange data
                tracked = self._positions[sym]
                tracked.qty = Decimal(str(contracts))
                tracked.unrealized_pnl = Decimal(str(p.get("unrealizedPnl", 0) or 0))

        # Remove positions that no longer exist on exchange
        stale = [s for s in self._positions if s not in exchange_syms]
        for s in stale:
            self._positions.pop(s)

    def to_dict(self) -> list[dict]:
        """Serialize for state persistence."""
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "qty": str(p.qty),
                "entry_price": str(p.entry_price),
                "leverage": p.leverage,
                "strategy_id": p.strategy_id,
                "opened_at": p.opened_at.isoformat(),
                "metadata": p.metadata,
            }
            for p in self._positions.values()
        ]
