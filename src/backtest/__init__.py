"""Universal strategy analyzer for backtesting.

Quick start:
    from src.backtest import run_backtest, compute_metrics
    from src.backtest.presets import bybit_futures
    from src.backtest.runner import EventStrategy

    class MyStrategy(EventStrategy):
        def find_events(self, data, **params):
            return [...]

    trades = run_backtest(MyStrategy(), data, cost_model=bybit_futures())
    m = compute_metrics(trades)
    print(m.summary())
"""
from .cost import CostModel
from .metrics import StrategyMetrics, compute_metrics
from .models import CostBreakdown, ExitReason, Side, Trade
from .runner import (
    CandleStrategy,
    EventStrategy,
    PortfolioStrategy,
    run_backtest,
)

__all__ = [
    "CostBreakdown",
    "CostModel",
    "ExitReason",
    "Side",
    "StrategyMetrics",
    "Trade",
    "CandleStrategy",
    "EventStrategy",
    "PortfolioStrategy",
    "compute_metrics",
    "run_backtest",
]
