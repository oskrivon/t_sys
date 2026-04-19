"""
Backtest: Volume Ranking Long/Short (Scott Phillips / Hyper Trend strategy).

Source: Odds on Open podcast transcript.

Rules:
  - Universe: top 50+ perps on Binance
  - For each coin: ratio = avg_volume_7d / avg_volume_30d
  - Long top 50% (volume accelerating), Short bottom 50% (decelerating)
  - Market-neutral (dollar-neutral), equal-weight
  - Daily rebalance

Claims: Sharpe 2.61, +49.4% in 11 months, MaxDD 9.4%

We test on: 50 symbols, 24 months, 4H candles (aggregate to daily).

Usage:
    python scripts/research/backtest_volume_ranking.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

DATA = ROOT / "data" / "processed" / "candles"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT", "TRX/USDT", "LINK/USDT", "ADA/USDT",
    "AVAX/USDT", "NEAR/USDT", "LTC/USDT", "FET/USDT", "UNI/USDT",
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "WIF/USDT", "ONDO/USDT", "RENDER/USDT",
    "ATOM/USDT", "ETC/USDT", "APT/USDT", "MANTA/USDT", "SEI/USDT",
    "JUP/USDT", "WLD/USDT", "STRK/USDT", "PENDLE/USDT", "ENA/USDT",
    "TAO/USDT", "GALA/USDT",
]

# Fees: Binance futures taker 0.04%, we assume taker for rebalance
FEE_PER_SIDE = 0.0004  # 4 bps


def load_daily_data() -> dict[str, pd.DataFrame]:
    """Load 4H data and resample to daily."""
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_4h.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")

        daily = df.resample("1D").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

        datasets[symbol] = daily
    return datasets


def compute_volume_ratios(datasets: dict[str, pd.DataFrame], date: pd.Timestamp,
                           short_window: int = 7, long_window: int = 30) -> dict[str, float]:
    """Compute volume ratio (7d/30d) for each symbol at given date."""
    ratios = {}
    for symbol, df in datasets.items():
        mask = df.index <= date
        if mask.sum() < long_window + 5:
            continue

        sub = df[mask]
        vol_short = sub["volume"].iloc[-short_window:].mean()
        vol_long = sub["volume"].iloc[-long_window:].mean()

        if vol_long > 0:
            ratios[symbol] = vol_short / vol_long
    return ratios


def backtest_volume_ranking(datasets: dict[str, pd.DataFrame],
                             short_window: int = 7,
                             long_window: int = 30,
                             fee_per_side: float = FEE_PER_SIDE,
                             top_pct: float = 0.5) -> pd.DataFrame:
    """Run the volume ranking long/short strategy."""

    # Find common date range
    all_dates = None
    for df in datasets.values():
        dates = set(df.index)
        if all_dates is None:
            all_dates = dates
        else:
            all_dates = all_dates.intersection(dates)

    all_dates = sorted(all_dates)
    # Skip warmup
    start_idx = long_window + 5

    daily_returns = []

    prev_longs = set()
    prev_shorts = set()

    for i in range(start_idx, len(all_dates)):
        date = all_dates[i]
        prev_date = all_dates[i - 1]

        # Compute volume ratios at yesterday's close
        ratios = compute_volume_ratios(datasets, prev_date, short_window, long_window)
        if len(ratios) < 10:
            continue

        # Rank
        sorted_symbols = sorted(ratios.keys(), key=lambda s: ratios[s], reverse=True)
        n = len(sorted_symbols)
        n_long = int(n * top_pct)
        n_short = n - n_long

        longs = set(sorted_symbols[:n_long])
        shorts = set(sorted_symbols[n_long:])

        # Compute daily returns for each position
        long_returns = []
        for sym in longs:
            if sym in datasets and date in datasets[sym].index and prev_date in datasets[sym].index:
                ret = datasets[sym].loc[date, "close"] / datasets[sym].loc[prev_date, "close"] - 1
                long_returns.append(ret)

        short_returns = []
        for sym in shorts:
            if sym in datasets and date in datasets[sym].index and prev_date in datasets[sym].index:
                ret = datasets[sym].loc[date, "close"] / datasets[sym].loc[prev_date, "close"] - 1
                short_returns.append(-ret)  # short = inverse return

        if not long_returns or not short_returns:
            continue

        # Portfolio return (equal-weight, dollar-neutral)
        long_ret = np.mean(long_returns)
        short_ret = np.mean(short_returns)
        gross_ret = (long_ret + short_ret) / 2  # half long, half short

        # Turnover cost
        new_longs = longs - prev_longs
        new_shorts = shorts - prev_shorts
        turnover = (len(new_longs) + len(new_shorts)) / max(1, n)
        fee_cost = turnover * fee_per_side * 2  # buy + sell

        net_ret = gross_ret - fee_cost

        daily_returns.append({
            "date": date,
            "gross_ret": gross_ret,
            "net_ret": net_ret,
            "long_ret": long_ret,
            "short_ret": short_ret,
            "fee_cost": fee_cost,
            "turnover": turnover,
            "n_symbols": n,
            "n_longs": len(long_returns),
            "n_shorts": len(short_returns),
        })

        prev_longs = longs
        prev_shorts = shorts

    return pd.DataFrame(daily_returns)


def analyze(result_df: pd.DataFrame, name: str):
    """Print strategy stats."""
    df = result_df.copy()
    n_days = len(df)
    n_months = n_days / 30

    for col, label in [("net_ret", "NET (after fees)"), ("gross_ret", "GROSS (before fees)")]:
        rets = df[col].values
        total_ret = np.prod(1 + rets) - 1
        annual_ret = (1 + total_ret) ** (365 / n_days) - 1
        daily_std = np.std(rets)
        sharpe = np.mean(rets) / daily_std * np.sqrt(365) if daily_std > 0 else 0

        # Max drawdown
        equity = np.cumprod(1 + rets)
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak
        max_dd = dd.max()

        # Win rate
        win_days = (rets > 0).sum()
        wr = win_days / n_days

        print(f"\n  {label}:")
        print(f"    Total return:  {total_ret*100:+.1f}% over {n_days} days ({n_months:.0f} months)")
        print(f"    Annual return: {annual_ret*100:+.1f}%")
        print(f"    Sharpe ratio:  {sharpe:.2f}")
        print(f"    Max drawdown:  {max_dd*100:.1f}%")
        print(f"    Win rate:      {wr*100:.0f}% of days")
        print(f"    Avg daily:     {np.mean(rets)*100:+.4f}%")
        print(f"    Std daily:     {daily_std*100:.4f}%")

    # Fee analysis
    avg_turnover = df["turnover"].mean()
    avg_fee = df["fee_cost"].mean()
    total_fees = df["fee_cost"].sum()
    print(f"\n  Fees & Turnover:")
    print(f"    Avg daily turnover: {avg_turnover*100:.1f}%")
    print(f"    Avg daily fee cost: {avg_fee*100:.4f}%")
    print(f"    Total fees paid:    {total_fees*100:.2f}%")

    # Monthly breakdown
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    print(f"\n  Monthly returns (net):")
    for month, group in df.groupby("month"):
        mret = np.prod(1 + group["net_ret"].values) - 1
        print(f"    {month}: {mret*100:+.2f}%")

    # Long vs Short contribution
    avg_long = df["long_ret"].mean()
    avg_short = df["short_ret"].mean()
    print(f"\n  Long vs Short:")
    print(f"    Avg daily long:  {avg_long*100:+.4f}%")
    print(f"    Avg daily short: {avg_short*100:+.4f}%")


def main():
    print("=== Volume Ranking Long/Short Backtest ===\n")

    datasets = load_daily_data()
    print(f"Loaded {len(datasets)} symbols\n")

    if not datasets:
        print("No data!")
        return

    # Main test: exact Scott Phillips rules
    print(f"{'='*60}")
    print(f"  Config: 7d/30d volume ratio, top/bottom 50%, daily rebalance")
    print(f"{'='*60}")

    result = backtest_volume_ranking(datasets, short_window=7, long_window=30)
    analyze(result, "7d/30d 50/50")

    # Sensitivity: different windows
    print(f"\n{'='*60}")
    print(f"  SENSITIVITY ANALYSIS")
    print(f"{'='*60}")

    configs = [
        ("3d/14d", 3, 14),
        ("7d/30d (baseline)", 7, 30),
        ("14d/60d", 14, 60),
        ("7d/14d", 7, 14),
    ]

    print(f"\n  {'Config':20s} {'Return':>8} {'Annual':>8} {'Sharpe':>7} {'MaxDD':>7} {'Turnover':>9}")
    for name, sw, lw in configs:
        r = backtest_volume_ranking(datasets, short_window=sw, long_window=lw)
        if r.empty:
            continue
        rets = r["net_ret"].values
        total = np.prod(1 + rets) - 1
        annual = (1 + total) ** (365 / len(r)) - 1
        sharpe = np.mean(rets) / np.std(rets) * np.sqrt(365) if np.std(rets) > 0 else 0
        equity = np.cumprod(1 + rets)
        peak = np.maximum.accumulate(equity)
        max_dd = ((peak - equity) / peak).max()
        avg_to = r["turnover"].mean()
        print(f"  {name:20s} {total*100:>+7.1f}% {annual*100:>+7.1f}% {sharpe:>6.2f} {max_dd*100:>6.1f}% {avg_to*100:>7.1f}%")

    # Fee sensitivity
    print(f"\n  Fee sensitivity (7d/30d):")
    print(f"  {'Fee':>10} {'Return':>8} {'Sharpe':>7}")
    for fee in [0.0, 0.0002, 0.0004, 0.0006, 0.001]:
        r = backtest_volume_ranking(datasets, fee_per_side=fee)
        rets = r["net_ret"].values
        total = np.prod(1 + rets) - 1
        sharpe = np.mean(rets) / np.std(rets) * np.sqrt(365) if np.std(rets) > 0 else 0
        print(f"  {fee*10000:>7.0f} bps {total*100:>+7.1f}% {sharpe:>6.2f}")


if __name__ == "__main__":
    main()
