"""Earnings backtest reporting: bucket analysis, comparison, equity curves."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import structlog

from src.backtest.metrics import StrategyMetrics, compute_metrics
from src.backtest.models import Trade

logger = structlog.get_logger()


# ── Capital-adjusted metrics for event-driven strategies ─────────────


@dataclass
class EventMetrics:
    """Honest metrics accounting for concurrent positions."""
    n_trades: int
    win_rate: float
    avg_return_pct: float        # per-trade
    median_return_pct: float
    std_return_pct: float        # per-trade
    sharpe_per_trade: float      # mean / std (single trade)
    sharpe_annualized: float     # per_trade * sqrt(trades/year)
    profit_factor: float
    max_concurrent: int
    avg_concurrent: float
    capital_required: float      # max_concurrent * position_size
    total_pnl_usd: float
    total_return_on_capital: float
    annual_return_on_capital: float
    trades_per_year: float
    years: float
    # Long/short breakdown
    n_long: int = 0
    n_short: int = 0
    long_avg_pct: float = 0.0
    short_avg_pct: float = 0.0
    long_wr: float = 0.0
    short_wr: float = 0.0


def compute_event_metrics(
    trades: list[Trade],
    position_size: float = 10_000.0,
) -> Optional[EventMetrics]:
    """Capital-adjusted metrics for concurrent event-driven trades."""
    if not trades:
        return None

    trades_sorted = sorted(trades, key=lambda t: t.entry_time)
    pnls = np.array([t.net_pnl_pct for t in trades_sorted])
    pnl_usd = np.array([t.net_pnl_usd for t in trades_sorted])

    # Period
    first_entry = min(t.entry_time for t in trades_sorted)
    last_exit = max(t.exit_time for t in trades_sorted)
    days = (last_exit - first_entry).days
    years = max(days / 365, 0.01)
    trades_per_year = len(trades_sorted) / years

    # Concurrency
    events = []
    for t in trades_sorted:
        events.append((t.entry_time, +1))
        events.append((t.exit_time, -1))
    events.sort(key=lambda x: x[0])

    concurrent = 0
    max_concurrent = 0
    daily_max: dict = {}
    for ts, delta in events:
        concurrent += delta
        max_concurrent = max(max_concurrent, concurrent)
        day = ts.date()
        daily_max[day] = max(daily_max.get(day, 0), concurrent)

    avg_concurrent = float(np.mean(list(daily_max.values()))) if daily_max else 0

    # Capital
    capital_required = max(max_concurrent, 1) * position_size
    total_pnl = float(pnl_usd.sum())
    total_return = total_pnl / capital_required if capital_required > 0 else 0
    annual_return = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0

    # Per-trade stats
    avg_ret = float(pnls.mean())
    std_ret = float(pnls.std()) if len(pnls) > 1 else 0.0
    sharpe_pt = avg_ret / std_ret if std_ret > 0 else 0.0
    sharpe_annual = sharpe_pt * sqrt(trades_per_year) if trades_per_year > 0 else 0.0

    wins = pnls > 0
    losses = pnls <= 0
    win_sum = float(pnls[wins].sum()) if wins.any() else 0.0
    loss_sum = float(abs(pnls[losses].sum())) if losses.any() else 1e-9
    pf = win_sum / loss_sum if loss_sum > 0 else float("inf")

    # Long/Short
    longs = [t for t in trades_sorted if t.side.value == "long"]
    shorts = [t for t in trades_sorted if t.side.value == "short"]
    l_pnls = [t.net_pnl_pct for t in longs]
    s_pnls = [t.net_pnl_pct for t in shorts]

    return EventMetrics(
        n_trades=len(trades_sorted),
        win_rate=float(wins.sum() / len(pnls)),
        avg_return_pct=avg_ret,
        median_return_pct=float(np.median(pnls)),
        std_return_pct=std_ret,
        sharpe_per_trade=sharpe_pt,
        sharpe_annualized=sharpe_annual,
        profit_factor=pf,
        max_concurrent=max_concurrent,
        avg_concurrent=avg_concurrent,
        capital_required=capital_required,
        total_pnl_usd=total_pnl,
        total_return_on_capital=total_return,
        annual_return_on_capital=annual_return,
        trades_per_year=trades_per_year,
        years=years,
        n_long=len(longs),
        n_short=len(shorts),
        long_avg_pct=float(np.mean(l_pnls)) if l_pnls else 0,
        short_avg_pct=float(np.mean(s_pnls)) if s_pnls else 0,
        long_wr=float(sum(1 for p in l_pnls if p > 0) / len(l_pnls)) if l_pnls else 0,
        short_wr=float(sum(1 for p in s_pnls if p > 0) / len(s_pnls)) if s_pnls else 0,
    )


def bucket_analysis(
    trades: list[Trade],
    n_buckets: int = 5,
    bucket_field: str = "eps_surprise_pct",
) -> pd.DataFrame:
    """Analyze performance by buckets of a metadata field.

    Returns DataFrame with columns:
        bucket, n_trades, win_rate, avg_return_pct, sharpe_per_trade,
        total_return_pct
    """
    if not trades:
        return pd.DataFrame()

    rows = []
    for t in trades:
        val = t.metadata.get(bucket_field)
        if val is None:
            continue
        rows.append({
            "value": float(val),
            "net_pnl_pct": t.net_pnl_pct,
            "side": t.side.value,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["bucket"] = pd.qcut(
        df["value"], q=n_buckets, duplicates="drop",
        labels=False,
    )

    # Also store bucket ranges for display
    bucket_edges = pd.qcut(df["value"], q=n_buckets, duplicates="drop")

    results = []
    for bucket_id in sorted(df["bucket"].unique()):
        mask = df["bucket"] == bucket_id
        group = df[mask]
        pnls = group["net_pnl_pct"].values

        bucket_label = str(bucket_edges[mask].cat.categories[0]) if hasattr(bucket_edges[mask], "cat") else f"Q{bucket_id + 1}"

        results.append({
            "bucket": f"Q{bucket_id + 1}",
            "range": bucket_label,
            "n_trades": len(group),
            "win_rate": float((pnls > 0).sum() / len(pnls)) if len(pnls) > 0 else 0,
            "avg_return_pct": float(pnls.mean()),
            "median_return_pct": float(np.median(pnls)),
            "std_return_pct": float(pnls.std()) if len(pnls) > 1 else 0,
            "total_return_pct": float(pnls.sum()),
        })

    return pd.DataFrame(results)


def comparison_table(
    results: dict[str, list[Trade]],
    position_size: float = 10_000,
) -> pd.DataFrame:
    """Compare multiple strategy variants side by side (capital-adjusted).

    Args:
        results: {strategy_name: trades_list}

    Returns DataFrame with one row per strategy.
    """
    rows = []
    for name, trades in results.items():
        if not trades:
            rows.append({"strategy": name, "n_trades": 0})
            continue

        m = compute_event_metrics(trades, position_size=position_size)
        if m is None:
            rows.append({"strategy": name, "n_trades": 0})
            continue

        rows.append({
            "strategy": name,
            "n_trades": m.n_trades,
            "win_rate": m.win_rate,
            "avg_ret_pct": m.avg_return_pct,
            "sharpe_pt": m.sharpe_per_trade,
            "sharpe_ann": m.sharpe_annualized,
            "pf": m.profit_factor,
            "max_conc": m.max_concurrent,
            "capital_k": m.capital_required / 1000,
            "annual_ret": m.annual_return_on_capital,
        })

    return pd.DataFrame(rows)


def hold_period_analysis(
    all_trades: dict[int, list[Trade]],
    position_size: float = 10_000,
) -> pd.DataFrame:
    """Compare performance across different hold periods (capital-adjusted).

    Args:
        all_trades: {hold_days: trades_list}
    """
    rows = []
    for hold_days, trades in sorted(all_trades.items()):
        if not trades:
            continue
        m = compute_event_metrics(trades, position_size=position_size)
        if m is None:
            continue
        rows.append({
            "hold_days": hold_days,
            "n_trades": m.n_trades,
            "win_rate": m.win_rate,
            "avg_ret_pct": m.avg_return_pct,
            "sharpe_pt": m.sharpe_per_trade,
            "sharpe_ann": m.sharpe_annualized,
            "max_conc": m.max_concurrent,
            "annual_ret": m.annual_return_on_capital,
        })

    return pd.DataFrame(rows)


def print_report(
    strategy_name: str,
    trades: list[Trade],
    bucket_df: Optional[pd.DataFrame] = None,
    position_size: float = 10_000,
) -> None:
    """Print formatted backtest report with capital-adjusted metrics."""
    if not trades:
        print(f"\n=== {strategy_name} ===")
        print("  No trades generated.")
        return

    m = compute_event_metrics(trades, position_size=position_size)
    if m is None:
        return

    print(f"\n{'=' * 60}")
    print(f"  {strategy_name}")
    print(f"{'=' * 60}")
    print(f"  Trades:          {m.n_trades} ({m.trades_per_year:.0f}/year, {m.years:.1f}y)")
    print(f"  Win Rate:        {m.win_rate:.1%}")
    print(f"  Avg Return:      {m.avg_return_pct:+.3%}  (median {m.median_return_pct:+.3%})")
    print(f"  Std per trade:   {m.std_return_pct:.3%}")
    print(f"  Profit Factor:   {m.profit_factor:.2f}")
    print(f"  Sharpe/trade:    {m.sharpe_per_trade:.3f}")
    print(f"  Sharpe annual:   {m.sharpe_annualized:.2f}")
    print(f"  --- Capital ---")
    print(f"  Max concurrent:  {m.max_concurrent}")
    print(f"  Avg concurrent:  {m.avg_concurrent:.1f}")
    print(f"  Capital needed:  ${m.capital_required:,.0f}")
    print(f"  Total PnL:       ${m.total_pnl_usd:+,.0f}")
    print(f"  Return/capital:  {m.total_return_on_capital:+.1%}")
    print(f"  Annual/capital:  {m.annual_return_on_capital:+.1%}")

    # Long/Short breakdown
    if m.n_long:
        print(f"\n  Long:  N={m.n_long:>4}  WR={m.long_wr:.0%}  Avg={m.long_avg_pct:+.3%}")
    if m.n_short:
        print(f"  Short: N={m.n_short:>4}  WR={m.short_wr:.0%}  Avg={m.short_avg_pct:+.3%}")

    if bucket_df is not None and not bucket_df.empty:
        print(f"\n  --- EPS Surprise Buckets ---")
        print(f"  {'Bucket':<8} {'N':>5} {'WR':>7} {'Avg%':>8} {'Tot%':>8}")
        for _, row in bucket_df.iterrows():
            print(
                f"  {row['bucket']:<8} {row['n_trades']:>5} "
                f"{row['win_rate']:>6.0%} {row['avg_return_pct']:>+7.3%} "
                f"{row['total_return_pct']:>+7.1%}"
            )

    print()


def save_equity_curve(
    trades: list[Trade],
    output_path: str = "data/reports/earnings_equity.png",
    title: str = "Earnings PEAD Equity Curve",
    initial_capital: float = 10_000,
) -> None:
    """Save equity curve plot as PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib_not_available")
        return

    if not trades:
        return

    metrics = compute_metrics(trades, initial_capital=initial_capital)
    eq = metrics.equity_curve

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(eq.index, eq.values, linewidth=1)
    ax.set_title(title)
    ax.set_ylabel("Equity ($)")
    ax.set_xlabel("Date")
    ax.grid(True, alpha=0.3)

    # Annotate key metrics
    text = (
        f"Sharpe: {metrics.sharpe:.2f}  |  WR: {metrics.win_rate:.0%}  |  "
        f"DD: {metrics.max_drawdown_pct:.1%}  |  "
        f"Annual: {metrics.annual_return_pct:.1%}"
    )
    ax.text(
        0.5, 0.02, text,
        transform=ax.transAxes, ha="center", fontsize=9, alpha=0.7,
    )

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    logger.info("equity_curve_saved", path=output_path)


def export_trades_csv(
    trades: list[Trade],
    output_path: str = "data/reports/earnings_trades.csv",
) -> None:
    """Export trade details to CSV."""
    if not trades:
        return

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for t in trades:
        rows.append({
            "symbol": t.symbol,
            "side": t.side.value,
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat(),
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "gross_pnl_pct": t.gross_pnl_pct,
            "net_pnl_pct": t.net_pnl_pct,
            "eps_surprise_pct": t.metadata.get("eps_surprise_pct"),
            "llm_score": t.metadata.get("llm_score"),
            "hold_days": t.metadata.get("hold_days"),
            "strategy_id": t.strategy_id,
        })

    pd.DataFrame(rows).to_csv(output_path, index=False)
    logger.info("trades_exported", path=output_path, count=len(rows))
