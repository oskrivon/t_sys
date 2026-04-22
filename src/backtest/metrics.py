"""Unified metrics calculator for backtested trades.

Usage:
    from src.backtest.metrics import compute_metrics
    m = compute_metrics(trades, initial_capital=10_000)
    print(f"WR: {m.win_rate:.0%}  Sharpe: {m.sharpe:.2f}")
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Optional

import numpy as np
import pandas as pd

from .models import CostBreakdown, Trade


@dataclass
class StrategyMetrics:
    """Complete metrics output from compute_metrics."""
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_pct: float          # avg net_pnl_pct
    sharpe: float                  # annualized on daily equity returns
    max_drawdown_pct: float
    annual_return_pct: float
    total_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_hold_hours: float
    trades_per_month: float
    # Aggregated costs
    cost_totals: dict[str, float]  # {entry_fee, exit_fee, slippage, ...} summed
    # Breakdowns
    monthly: pd.DataFrame          # [month, return_pct, n_trades, win_rate]
    per_symbol: pd.DataFrame       # [symbol, n_trades, win_rate, net_pnl_pct]
    equity_curve: pd.Series        # datetime-indexed cumulative equity

    def summary(self) -> str:
        """One-line summary string."""
        return (
            f"N={self.n_trades}  WR={self.win_rate:.0%}  "
            f"PF={self.profit_factor:.2f}  Exp={self.expectancy_pct:.2%}  "
            f"Sharpe={self.sharpe:.2f}  DD={self.max_drawdown_pct:.1%}  "
            f"Annual={self.annual_return_pct:.1%}  T/mo={self.trades_per_month:.1f}"
        )

    def cost_summary(self) -> str:
        """Breakdown of costs in bps."""
        parts = []
        for name, val in self.cost_totals.items():
            if val != 0:
                avg_bps = val / max(self.n_trades, 1) * 10_000
                parts.append(f"{name}={avg_bps:.1f}bps/trade")
        return "  ".join(parts)


def compute_metrics(
    trades: list[Trade],
    initial_capital: float = 10_000,
    risk_free_rate: float = 0.0,
) -> StrategyMetrics:
    """Pure function: list[Trade] -> StrategyMetrics."""
    if not trades:
        return _empty_metrics()

    # Sort by exit time
    trades_sorted = sorted(trades, key=lambda t: t.exit_time)

    # Basic arrays
    net_pnls = np.array([t.net_pnl_pct for t in trades_sorted])
    net_usd = np.array([t.net_pnl_usd for t in trades_sorted])
    gross_pnls = np.array([t.gross_pnl_pct for t in trades_sorted])
    wins = net_pnls > 0
    losses = net_pnls <= 0

    n = len(trades_sorted)
    win_rate = float(wins.sum() / n)

    # Profit factor
    gross_wins = float(net_pnls[wins].sum()) if wins.any() else 0.0
    gross_losses = float(abs(net_pnls[losses].sum())) if losses.any() else 1e-9
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Win/loss averages
    avg_win = float(net_pnls[wins].mean()) if wins.any() else 0.0
    avg_loss = float(net_pnls[losses].mean()) if losses.any() else 0.0
    expectancy = float(net_pnls.mean())

    # Hold duration
    hold_hours = np.array([
        (t.exit_time - t.entry_time).total_seconds() / 3600
        for t in trades_sorted
    ])
    avg_hold = float(hold_hours.mean())

    # Equity curve (datetime-indexed)
    equity = np.cumsum(net_usd) + initial_capital
    exit_times = [t.exit_time for t in trades_sorted]
    equity_series = pd.Series(equity, index=pd.DatetimeIndex(exit_times))

    # Resample to daily for Sharpe / drawdown
    daily_equity = equity_series.resample("D").last().ffill()
    daily_equity.iloc[0] = initial_capital  # ensure start
    daily_returns = daily_equity.pct_change().dropna()

    # Sharpe (annualized)
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = float(
            (daily_returns.mean() - risk_free_rate / 365)
            / daily_returns.std() * sqrt(365)
        )
    else:
        sharpe = 0.0

    # Max drawdown
    peak = daily_equity.cummax()
    drawdown = (daily_equity - peak) / peak
    max_dd = float(drawdown.min())

    # Total / annual return
    total_return = float((equity[-1] - initial_capital) / initial_capital)
    days = (trades_sorted[-1].exit_time - trades_sorted[0].entry_time).days
    if days > 0:
        annual_return = (1 + total_return) ** (365 / days) - 1
    else:
        annual_return = total_return
    annual_return = float(annual_return)

    # Trades per month
    months = max(days / 30.44, 1)
    trades_per_month = n / months

    # Aggregate costs
    cost_totals = _aggregate_costs(trades_sorted)

    # Monthly breakdown
    monthly = _monthly_breakdown(trades_sorted)

    # Per-symbol breakdown
    per_symbol = _per_symbol_breakdown(trades_sorted)

    return StrategyMetrics(
        n_trades=n,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_pct=expectancy,
        sharpe=sharpe,
        max_drawdown_pct=max_dd,
        annual_return_pct=annual_return,
        total_return_pct=total_return,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        avg_hold_hours=avg_hold,
        trades_per_month=trades_per_month,
        cost_totals=cost_totals,
        monthly=monthly,
        per_symbol=per_symbol,
        equity_curve=equity_series,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _aggregate_costs(trades: list[Trade]) -> dict[str, float]:
    totals: dict[str, float] = {
        "entry_fee": 0.0, "exit_fee": 0.0, "slippage": 0.0,
        "spread": 0.0, "funding": 0.0, "borrow": 0.0,
    }
    for t in trades:
        c = t.costs
        totals["entry_fee"] += c.entry_fee
        totals["exit_fee"] += c.exit_fee
        totals["slippage"] += c.slippage
        totals["spread"] += c.spread
        totals["funding"] += c.funding
        totals["borrow"] += c.borrow
    return totals


def _monthly_breakdown(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["month", "return_pct", "n_trades", "win_rate"])
    rows = []
    for t in trades:
        rows.append({
            "month": t.exit_time.strftime("%Y-%m"),
            "net_pnl_pct": t.net_pnl_pct,
            "win": 1 if t.net_pnl_pct > 0 else 0,
        })
    df = pd.DataFrame(rows)
    grouped = df.groupby("month").agg(
        return_pct=("net_pnl_pct", "sum"),
        n_trades=("net_pnl_pct", "count"),
        win_rate=("win", "mean"),
    ).reset_index()
    return grouped


def _per_symbol_breakdown(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["symbol", "n_trades", "win_rate", "net_pnl_pct"])
    rows = []
    for t in trades:
        rows.append({
            "symbol": t.symbol,
            "net_pnl_pct": t.net_pnl_pct,
            "win": 1 if t.net_pnl_pct > 0 else 0,
        })
    df = pd.DataFrame(rows)
    grouped = df.groupby("symbol").agg(
        n_trades=("net_pnl_pct", "count"),
        win_rate=("win", "mean"),
        net_pnl_pct=("net_pnl_pct", "sum"),
    ).reset_index().sort_values("net_pnl_pct", ascending=False)
    return grouped


def _empty_metrics() -> StrategyMetrics:
    return StrategyMetrics(
        n_trades=0, win_rate=0.0, profit_factor=0.0, expectancy_pct=0.0,
        sharpe=0.0, max_drawdown_pct=0.0, annual_return_pct=0.0,
        total_return_pct=0.0, avg_win_pct=0.0, avg_loss_pct=0.0,
        avg_hold_hours=0.0, trades_per_month=0.0,
        cost_totals={}, monthly=pd.DataFrame(), per_symbol=pd.DataFrame(),
        equity_curve=pd.Series(dtype=float),
    )
