"""Pluggable cost model pipeline for backtesting.

Usage:
    model = CostModel([TakerFee(0.00055), FixedSlippage(0.0001)])
    costed_trades = model.apply_all(raw_trades)
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, Sequence

from .models import CostBreakdown, Trade


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class CostComponent(Protocol):
    """Single cost step. Returns updated CostBreakdown."""
    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown: ...


# ---------------------------------------------------------------------------
# Fee components
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TakerFee:
    """Taker fee applied to both entry and exit."""
    rate: float  # per side, e.g. 0.00055 for 5.5 bps

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        return replace(current,
                       entry_fee=current.entry_fee + self.rate,
                       exit_fee=current.exit_fee + self.rate)


@dataclass(frozen=True)
class MakerFee:
    """Maker fee (or rebate if negative) applied to both sides."""
    rate: float

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        return replace(current,
                       entry_fee=current.entry_fee + self.rate,
                       exit_fee=current.exit_fee + self.rate)


@dataclass(frozen=True)
class MixedFee:
    """Different fee for entry (e.g. taker) and exit (e.g. maker)."""
    entry_rate: float
    exit_rate: float

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        return replace(current,
                       entry_fee=current.entry_fee + self.entry_rate,
                       exit_fee=current.exit_fee + self.exit_rate)


# ---------------------------------------------------------------------------
# Slippage components
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FixedSlippage:
    """Fixed slippage per side."""
    rate: float  # per side, e.g. 0.0001 for 1 bps

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        return replace(current, slippage=current.slippage + 2 * self.rate)


@dataclass(frozen=True)
class VolBasedSlippage:
    """Slippage proportional to recent volatility.

    Reads ``trade.metadata["volatility_1m"]`` (fractional 1-minute range).
    Falls back to ``default`` if key is missing.
    """
    multiplier: float = 0.5
    default: float = 0.0005  # 5 bps fallback

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        vol = trade.metadata.get("volatility_1m", self.default)
        slip = self.multiplier * vol * 2  # both sides
        return replace(current, slippage=current.slippage + slip)


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SpreadCost:
    """Half-spread crossing cost per side."""
    half_spread: float  # e.g. 0.00005 for 0.5 bps half-spread

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        return replace(current, spread=current.spread + 2 * self.half_spread)


# ---------------------------------------------------------------------------
# Holding costs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FundingDuringHold:
    """Flat funding rate charged per interval while position is open.

    For shorts in positive funding: you RECEIVE funding → set rate negative
    or handle sign in your strategy. This component always ADDS the cost.
    """
    rate_per_interval: float = 0.0001  # 1 bps per 8h default
    interval_hours: float = 8.0

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        hold_hours = (trade.exit_time - trade.entry_time).total_seconds() / 3600
        periods = hold_hours / self.interval_hours
        cost = self.rate_per_interval * periods
        return replace(current, funding=current.funding + cost)


@dataclass(frozen=True)
class HistoricalFunding:
    """Use actual historical funding rates from trade metadata.

    Expects ``trade.metadata["funding_rates"]`` to be a list of
    ``(datetime, rate)`` tuples covering the hold period.
    Positive rate = long pays, short receives.
    """
    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        rates = trade.metadata.get("funding_rates", [])
        if not rates:
            return current
        total = 0.0
        for _ts, rate in rates:
            # Long pays positive funding, receives negative
            # Short pays negative funding, receives positive
            if trade.side.value == "long":
                total += rate
            else:
                total -= rate
        return replace(current, funding=current.funding + total)


@dataclass(frozen=True)
class BorrowCost:
    """Annualized borrow cost, pro-rated by hold duration."""
    annual_rate: float = 0.10  # 10% APR default

    def apply(self, trade: Trade, current: CostBreakdown) -> CostBreakdown:
        hold_hours = (trade.exit_time - trade.entry_time).total_seconds() / 3600
        cost = self.annual_rate * hold_hours / 8760
        return replace(current, borrow=current.borrow + cost)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class CostModel:
    """Pipeline of cost components applied sequentially to trades."""

    def __init__(self, components: Sequence[CostComponent] = ()) -> None:
        self._components = list(components)

    def add(self, component: CostComponent) -> CostModel:
        """Return new CostModel with an additional component."""
        return CostModel(self._components + [component])

    def apply(self, trade: Trade) -> Trade:
        """Apply all cost components, update trade's costs and net P&L."""
        breakdown = CostBreakdown()
        for comp in self._components:
            breakdown = comp.apply(trade, breakdown)
        trade.costs = breakdown
        trade.recompute()
        return trade

    def apply_all(self, trades: list[Trade]) -> list[Trade]:
        """Apply costs to every trade in the list (mutates in place)."""
        for t in trades:
            self.apply(t)
        return trades

    def describe(self) -> list[str]:
        """Human-readable description of components."""
        lines = []
        for c in self._components:
            name = type(c).__name__
            params = {k: v for k, v in c.__dict__.items() if not k.startswith("_")}
            if params:
                p = ", ".join(f"{k}={v}" for k, v in params.items())
                lines.append(f"{name}({p})")
            else:
                lines.append(name)
        return lines
