#!/usr/bin/env python3
"""Run multi-strategy portfolio.

Usage:
    python scripts/run_portfolio.py                    # paper trading, all strategies
    python scripts/run_portfolio.py --strategy miro    # only Miro
    python scripts/run_portfolio.py --capital 5000     # custom capital
    python scripts/run_portfolio.py --status           # show status and exit

Configuration is loaded from config/strategies.yaml (or defaults).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog
import ccxt.async_support as ccxt

from src.portfolio.manager import PortfolioManager
from src.strategies.base import StrategyConfig, StrategyType
from src.strategies.miro_strategy import MiroStrategy
from src.strategies.volume_ranking import VolumeRankingStrategy
from src.paper_trading.tracker import PaperTrader

log = structlog.get_logger()

# Default strategy configs
DEFAULT_STRATEGIES = [
    StrategyConfig(
        strategy_id="miro",
        name="Miro S/R Levels + ML + Vision",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=40,
        max_positions=3,
        risk_per_trade_pct=4.0,
        schedule_cron="4h",
        params={"vision_min_score": 8},
    ),
    StrategyConfig(
        strategy_id="volume_ranking",
        name="Volume Ranking Long/Short",
        strategy_type=StrategyType.SYSTEMATIC,
        allocation_pct=60,
        max_positions=50,
        risk_per_trade_pct=2.0,  # per symbol
        schedule_cron="1d",
        params={"short_window": 7, "long_window": 30, "top_pct": 0.5},
    ),
]

STRATEGY_CLASSES = {
    "miro": MiroStrategy,
    "volume_ranking": VolumeRankingStrategy,
}


def create_portfolio(capital: float, strategy_filter: str = None) -> PortfolioManager:
    """Create portfolio with configured strategies."""
    pm = PortfolioManager(total_capital=capital)

    for config in DEFAULT_STRATEGIES:
        if strategy_filter and config.strategy_id != strategy_filter:
            continue
        cls = STRATEGY_CLASSES.get(config.strategy_id)
        if cls:
            strategy = cls(config)
            pm.register_strategy(strategy)

    return pm


async def run_once(pm: PortfolioManager, exchange, paper_trader: PaperTrader):
    """Run all strategies once."""
    for sid in pm.strategies:
        signals = await pm.run_strategy(sid, exchange)
        for signal in signals:
            # Record in paper trading
            from src.strategy.models import Signal, SignalType, Level
            if hasattr(signal, "entry_price"):  # TradeSignal
                log.info("signal",
                         strategy=sid,
                         symbol=signal.symbol,
                         side=signal.side.value,
                         entry=signal.entry_price,
                         confidence=signal.confidence)


async def show_status(pm: PortfolioManager):
    """Show portfolio status."""
    status = pm.get_status()
    print(f"\n=== Portfolio Status ===")
    print(f"Capital: ${status['total_capital']:,.0f}")
    print(f"Total PnL: {status['total_pnl_pct']:+.2f}%")
    print(f"Total trades: {status['total_trades']}")

    for sid, s in status["strategies"].items():
        print(f"\n  [{sid}] {s.get('name', sid)}")
        print(f"    Type: {s.get('type', '?')}")
        print(f"    Allocation: {s['allocation_pct']:.0f}% (${s['capital']:,.0f})")
        print(f"    Open: {s['open_positions']} positions")
        print(f"    Trades: {s['trades']}, WR: {s['win_rate']*100:.0f}%")
        print(f"    PnL: {s['total_pnl_pct']:+.2f}%")


def main():
    parser = argparse.ArgumentParser(description="Multi-strategy portfolio runner")
    parser.add_argument("--capital", type=float, default=10_000)
    parser.add_argument("--strategy", type=str, default=None, help="Run only this strategy")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    args = parser.parse_args()

    pm = create_portfolio(args.capital, args.strategy)

    if args.status:
        asyncio.run(show_status(pm))
        return

    print(f"Portfolio: ${args.capital:,.0f}, {len(pm.strategies)} strategies")
    for sid, s in pm.strategies.items():
        print(f"  [{sid}] {s.config.name} ({s.config.allocation_pct}%)")

    print("\nReady. Use --status to check state.")
    print("Strategies run on schedule (4h for Miro, daily for VolRanking).")


if __name__ == "__main__":
    main()
