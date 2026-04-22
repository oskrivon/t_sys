"""Exchange-specific cost model presets.

Usage:
    from src.backtest.presets import bybit_futures
    model = bybit_futures()
"""
from __future__ import annotations

from .cost import (
    CostModel,
    TakerFee,
    MakerFee,
    MixedFee,
    FixedSlippage,
    VolBasedSlippage,
    SpreadCost,
    FundingDuringHold,
)


def bybit_futures(funding_rate: float = 0.0001) -> CostModel:
    """Bybit linear perps, VIP0 taker both sides.

    Taker: 5.5 bps/side, slippage ~1 bps/side, spread ~0.5 bps/side.
    """
    return CostModel([
        TakerFee(0.00055),
        FixedSlippage(0.0001),
        SpreadCost(0.00005),
        FundingDuringHold(funding_rate),
    ])


def bybit_futures_maker(funding_rate: float = 0.0001) -> CostModel:
    """Bybit with maker entry, taker exit."""
    return CostModel([
        MixedFee(entry_rate=0.0002, exit_rate=0.00055),
        FixedSlippage(0.00005),
        SpreadCost(0.00005),
        FundingDuringHold(funding_rate),
    ])


def bybit_funding_capture() -> CostModel:
    """Funding capture: 5-second holds, vol-based slippage, no funding cost."""
    return CostModel([
        TakerFee(0.00055),
        VolBasedSlippage(multiplier=0.5, default=0.0005),
    ])


def binance_futures(funding_rate: float = 0.0001) -> CostModel:
    """Binance USDM perps, VIP0 taker.

    Taker: 4 bps/side.
    """
    return CostModel([
        TakerFee(0.0004),
        FixedSlippage(0.0001),
        SpreadCost(0.00005),
        FundingDuringHold(funding_rate),
    ])


def binance_futures_maker(funding_rate: float = 0.0001) -> CostModel:
    """Binance USDM with maker entry (rebate -2.5 bps), taker exit."""
    return CostModel([
        MixedFee(entry_rate=-0.00025, exit_rate=0.0004),
        FixedSlippage(0.00005),
        SpreadCost(0.00005),
        FundingDuringHold(funding_rate),
    ])


def binance_spot() -> CostModel:
    """Binance spot, VIP0 taker."""
    return CostModel([
        TakerFee(0.001),
        FixedSlippage(0.0001),
        SpreadCost(0.0001),
    ])


def zero_cost() -> CostModel:
    """No costs — for debugging or comparing gross vs net."""
    return CostModel([])
