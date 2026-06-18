"""L2 order-book reconstruction from snapshot + incremental diffs.

Exchange-agnostic. A feed seeds the book with a ``snapshot`` then streams
``delta`` updates (changed price levels; qty 0 = remove). `BookState` keeps the
running book, detects sequence problems, and exposes the primitives the
wall-eating detector needs (best bid/ask, walls ≥ $N).

Sequence model (matches Bybit v5 orderbook + most venues):
  * ``snapshot`` resets the book and sets the baseline update id.
  * ``delta`` must carry a strictly increasing update id. A delta arriving
    before any snapshot, or with a non-increasing id, is rejected and flags
    ``resync_needed`` — the collector then re-seeds from a REST snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class Wall:
    """A resting level whose notional (price × size) clears a threshold."""
    side: str          # "bid" | "ask"
    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class BookState:
    """Running L2 book for one (exchange, symbol)."""

    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: Optional[int] = None
    seeded: bool = False
    resync_needed: bool = False
    # Diagnostics
    applied_snapshots: int = 0
    applied_deltas: int = 0
    rejected: int = 0

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def apply_snapshot(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        update_id: Optional[int] = None,
    ) -> None:
        """Replace the whole book from a fresh snapshot."""
        self.bids = {float(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in asks if float(s) > 0}
        self.last_update_id = update_id
        self.seeded = True
        self.resync_needed = False
        self.applied_snapshots += 1

    def apply_delta(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        update_id: Optional[int] = None,
    ) -> bool:
        """Apply an incremental update. Returns True if applied, False if rejected.

        A rejection sets ``resync_needed`` so the caller can re-seed.
        """
        if not self.seeded:
            self.resync_needed = True
            self.rejected += 1
            return False
        if (
            update_id is not None
            and self.last_update_id is not None
            and update_id <= self.last_update_id
        ):
            # Stale or duplicate — not necessarily a gap, but never apply backwards.
            self.rejected += 1
            return False

        for p, s in bids:
            self._apply_level(self.bids, float(p), float(s))
        for p, s in asks:
            self._apply_level(self.asks, float(p), float(s))

        if update_id is not None:
            self.last_update_id = update_id
        self.applied_deltas += 1
        return True

    @staticmethod
    def _apply_level(side: dict[float, float], price: float, size: float) -> None:
        if size <= 0:
            side.pop(price, None)
        else:
            side[price] = size

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (b + a) / 2.0

    @property
    def spread(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return a - b

    def is_crossed(self) -> bool:
        """True if best bid >= best ask — a corrupt book that needs resync."""
        b, a = self.best_bid, self.best_ask
        return b is not None and a is not None and b >= a

    def top(self, n: int, side: str) -> list[tuple[float, float]]:
        """Top-N levels of a side as (price, size), best price first."""
        if side == "bid":
            prices = sorted(self.bids, reverse=True)[:n]
            return [(p, self.bids[p]) for p in prices]
        if side == "ask":
            prices = sorted(self.asks)[:n]
            return [(p, self.asks[p]) for p in prices]
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")

    def walls(self, min_notional_usd: float) -> list[Wall]:
        """All levels whose price×size ≥ threshold, sorted by notional desc."""
        out: list[Wall] = []
        for p, s in self.bids.items():
            if p * s >= min_notional_usd:
                out.append(Wall("bid", p, s))
        for p, s in self.asks.items():
            if p * s >= min_notional_usd:
                out.append(Wall("ask", p, s))
        out.sort(key=lambda w: w.notional, reverse=True)
        return out
