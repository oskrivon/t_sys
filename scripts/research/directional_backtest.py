"""
Простой directional backtest: скачать OHLCV + прогнать базовые стратегии.

Стратегии:
  1. Momentum — buy after N green candles, sell after M red
  2. Mean reversion — buy при RSI < 30, sell при RSI > 70
  3. Breakout — buy при пробое high за N периодов

Пример:
    python scripts/research/directional_backtest.py
"""
from __future__ import annotations

import time
import sys
from pathlib import Path
from datetime import datetime, timezone

import ccxt
import pandas as pd
import numpy as np

DATA = Path("data/processed/candles")
REPORTS = Path("data/reports")

EXCHANGE = "binance"
SYMBOLS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAMES = ["1h"]  # start with 1h, add 5m later if needed
MONTHS_BACK = 6
FEE_BPS = 10  # spot taker


def fetch_ohlcv(symbol: str, timeframe: str, since_ms: int, limit: int = 1000) -> pd.DataFrame:
    """Fetch OHLCV via CCXT with pagination."""
    exchange = ccxt.binance({"enableRateLimit": True})
    all_candles = []
    current_since = since_ms

    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=limit)
        except Exception as e:
            print(f"  Error: {e}")
            break

        if not candles:
            break

        all_candles.extend(candles)
        last_ts = candles[-1][0]
        if last_ts <= current_since:
            break
        current_since = last_ts + 1

        sys.stdout.write(f"\r  {symbol} {timeframe}: {len(all_candles):,} candles")
        sys.stdout.flush()

        if len(candles) < limit:
            break
        time.sleep(0.1)

    sys.stdout.write(f"\r  {symbol} {timeframe}: {len(all_candles):,} candles done\n")

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    return df


def download_all() -> dict[str, pd.DataFrame]:
    """Download OHLCV for all symbols and timeframes."""
    DATA.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    since_ms = int((now.timestamp() - MONTHS_BACK * 30 * 86400) * 1000)

    datasets = {}
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            key = f"{symbol.replace('/', '')}_{tf}"
            cache_path = DATA / f"{key}.parquet"

            if cache_path.exists():
                print(f"  Loading cached: {cache_path}")
                df = pd.read_parquet(cache_path)
            else:
                print(f"  Fetching {symbol} {tf}...")
                df = fetch_ohlcv(symbol, tf, since_ms)
                if not df.empty:
                    df.to_parquet(cache_path, index=False)
                    print(f"  Saved: {cache_path} ({len(df)} candles)")

            if not df.empty:
                datasets[key] = df

    return datasets


# ── Indicators ──

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


# ── Strategies ──

def strategy_momentum(df: pd.DataFrame, lookback: int = 24, hold: int = 12) -> pd.Series:
    """Buy when price > SMA(lookback), sell when price < SMA(lookback).
    Simple trend-following.
    """
    ma = sma(df["close"], lookback)
    # 1 = long, 0 = flat
    signal = (df["close"] > ma).astype(int)
    return signal


def strategy_mean_reversion(df: pd.DataFrame, rsi_period: int = 14) -> pd.Series:
    """Buy when RSI < 30, sell when RSI > 70."""
    r = rsi(df["close"], rsi_period)
    position = pd.Series(0, index=df.index)
    in_position = False
    for i in range(len(df)):
        if not in_position and r.iloc[i] < 30:
            in_position = True
        elif in_position and r.iloc[i] > 70:
            in_position = False
        position.iloc[i] = 1 if in_position else 0
    return position


def strategy_breakout(df: pd.DataFrame, lookback: int = 48, hold: int = 12) -> pd.Series:
    """Buy when close > highest high of last N candles."""
    highest = df["high"].rolling(lookback).max().shift(1)
    signal = pd.Series(0, index=df.index)
    hold_count = 0
    for i in range(lookback, len(df)):
        if hold_count > 0:
            signal.iloc[i] = 1
            hold_count -= 1
        elif df["close"].iloc[i] > highest.iloc[i]:
            signal.iloc[i] = 1
            hold_count = hold
    return signal


def strategy_dual_ma(df: pd.DataFrame, fast: int = 12, slow: int = 48) -> pd.Series:
    """Golden/death cross: buy when fast EMA > slow EMA."""
    fast_ma = ema(df["close"], fast)
    slow_ma = ema(df["close"], slow)
    return (fast_ma > slow_ma).astype(int)


def strategy_buy_hold(df: pd.DataFrame) -> pd.Series:
    """Baseline: always long."""
    return pd.Series(1, index=df.index)


# ── Backtester ──

def backtest(df: pd.DataFrame, signals: pd.Series, fee_bps: float = FEE_BPS) -> dict:
    """Simple vectorized backtest.

    signals: 1=long, 0=flat. Fee charged on each entry/exit.
    """
    returns = df["close"].pct_change().fillna(0)

    # Detect trades (signal changes)
    trades = signals.diff().fillna(0).abs()
    fee_drag = trades * fee_bps / 10_000

    # Strategy returns
    strat_returns = signals.shift(1).fillna(0) * returns - fee_drag
    equity = (1 + strat_returns).cumprod()

    # Metrics
    total_return = equity.iloc[-1] - 1
    n_days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 86400
    apr = total_return / n_days * 365 if n_days > 0 else 0

    # Max drawdown
    peak = equity.cummax()
    drawdown = (equity - peak) / peak
    max_dd = drawdown.min()

    # Number of trades
    n_trades = int(trades.sum() / 2)  # entry + exit = 2 signals

    # Sharpe (annualized, hourly returns)
    if strat_returns.std() > 0:
        sharpe = strat_returns.mean() / strat_returns.std() * np.sqrt(8760)  # hourly
    else:
        sharpe = 0

    # Win rate (per-trade)
    # Group returns by trade
    trade_mask = signals.shift(1).fillna(0) > 0
    if trade_mask.any():
        trade_returns = returns[trade_mask]
        win_rate = (trade_returns > 0).mean()
    else:
        win_rate = 0

    return {
        "total_return": total_return,
        "apr": apr,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "n_days": n_days,
    }


STRATEGIES = {
    "buy_hold": strategy_buy_hold,
    "momentum_24h": lambda df: strategy_momentum(df, lookback=24),
    "momentum_72h": lambda df: strategy_momentum(df, lookback=72),
    "dual_ma_12_48": lambda df: strategy_dual_ma(df, fast=12, slow=48),
    "mean_reversion_14": lambda df: strategy_mean_reversion(df, rsi_period=14),
    "breakout_48h": lambda df: strategy_breakout(df, lookback=48, hold=12),
}


def run() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Directional Strategy Backtest ===\n")

    # Download data
    print("Downloading OHLCV...")
    datasets = download_all()

    if not datasets:
        print("No data!")
        return

    # Run backtests
    all_results = []

    for key, df in datasets.items():
        print(f"\n{'='*60}")
        print(f"  {key}: {len(df)} candles, {df['ts'].iloc[0].date()} to {df['ts'].iloc[-1].date()}")
        print(f"{'='*60}")

        for strat_name, strat_fn in STRATEGIES.items():
            signals = strat_fn(df)
            result = backtest(df, signals)
            result["dataset"] = key
            result["strategy"] = strat_name
            all_results.append(result)

            color = "+" if result["total_return"] > 0 else ""
            print(f"  {strat_name:25s}: return={color}{result['total_return']*100:.1f}%, "
                  f"APR={result['apr']*100:.1f}%, DD={result['max_drawdown']*100:.1f}%, "
                  f"sharpe={result['sharpe']:.2f}, trades={result['n_trades']}")

    # Report
    _write_report(all_results)


def _write_report(results: list[dict]) -> None:
    df = pd.DataFrame(results)

    lines = [
        "# Directional Strategy Backtest",
        "",
        f"**Exchange:** Binance spot",
        f"**Fee:** {FEE_BPS} bps per trade (taker)",
        f"**Timeframe:** 1h candles",
        "",
        "## Results",
        "",
        "| Dataset | Strategy | Return | APR | Max DD | Sharpe | Trades | Win% |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]

    for _, r in df.iterrows():
        lines.append(
            f"| {r['dataset']} | {r['strategy']} | "
            f"{r['total_return']*100:+.1f}% | {r['apr']*100:+.1f}% | "
            f"{r['max_drawdown']*100:.1f}% | {r['sharpe']:.2f} | "
            f"{int(r['n_trades'])} | {r['win_rate']*100:.1f}% |"
        )

    # Analysis
    lines += [
        "",
        "## Analysis",
        "",
        "**Baseline:** buy_hold shows the market return — any strategy must beat this.",
        "",
        "**Key questions:**",
        "1. Does ANY strategy beat buy_hold after fees?",
        "2. Is Sharpe > 1.0 for any strategy? (institutional threshold)",
        "3. Is max drawdown manageable (< 20%)?",
        "",
        "**If no strategy beats buy_hold**, directional alpha is hard to find with simple signals.",
        "More sophisticated approaches needed: ML, pattern recognition, event-driven.",
        "",
    ]

    out = REPORTS / "directional_backtest.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    run()
