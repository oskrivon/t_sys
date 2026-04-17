"""
Backtest Miro стратегии: пробой + ретест горизонтальных уровней.

Паттерны:
  1. Пробой — тело свечи закрывается за уровнем S/R
  2. Ретест — после пробоя цена возвращается к уровню, отскакивает → вход
  3. Закол — ложный пробой (выход за уровень без закрепления)

Risk management:
  - R:R минимум 1:3
  - SL за уровень (ширина зоны)
  - Max 5% депо на сделку

Пример:
    python scripts/research/miro_strategy_backtest.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np

DATA = Path("data/processed/candles")
REPORTS = Path("data/reports")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
]
TIMEFRAME = "4h"
MONTHS_BACK = 6
RR_RATIO = 3.0       # Risk:Reward 1:3
MAX_RISK_PCT = 5.0    # Max 5% deposit per trade
INITIAL_CAPITAL = 10_000
FEE_BPS = 10          # Taker fee per side


# ── Data download ──

def fetch_ohlcv(symbol: str, timeframe: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_candles = []
    current = since_ms

    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=current, limit=1000)
        except Exception as e:
            print(f"  Error {symbol}: {e}")
            break
        if not candles:
            break
        all_candles.extend(candles)
        last = candles[-1][0]
        if last <= current or len(candles) < 1000:
            break
        current = last + 1
        time.sleep(0.1)

    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def load_data() -> dict[str, pd.DataFrame]:
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{TIMEFRAME}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
        else:
            print(f"  Downloading {symbol} {TIMEFRAME}...")
            df = fetch_ohlcv(symbol, TIMEFRAME, MONTHS_BACK)
            if not df.empty:
                df.to_parquet(path, index=False)
                sys.stdout.write(f"  {symbol}: {len(df)} candles\n")
        if not df.empty:
            datasets[symbol] = df
    return datasets


# ── Level detection ──

def find_swing_points(df: pd.DataFrame, order: int = 5) -> tuple[pd.Series, pd.Series]:
    """Find swing highs and lows using local extrema."""
    highs = pd.Series(np.nan, index=df.index)
    lows = pd.Series(np.nan, index=df.index)

    for i in range(order, len(df) - order):
        # Swing high: high[i] is highest in window
        window_high = df["high"].iloc[i - order:i + order + 1]
        if df["high"].iloc[i] == window_high.max():
            highs.iloc[i] = df["high"].iloc[i]

        # Swing low: low[i] is lowest in window
        window_low = df["low"].iloc[i - order:i + order + 1]
        if df["low"].iloc[i] == window_low.min():
            lows.iloc[i] = df["low"].iloc[i]

    return highs.dropna(), lows.dropna()


def cluster_levels(
    swing_points: pd.Series,
    price_tolerance_pct: float = 1.5,
    min_touches: int = 2,
) -> list[dict]:
    """Cluster swing points into horizontal levels.

    Groups points within price_tolerance_pct of each other.
    Returns levels with >= min_touches.
    """
    if swing_points.empty:
        return []

    values = swing_points.sort_values().values
    indices = swing_points.sort_values().index.tolist()

    levels = []
    used = set()

    for i, price in enumerate(values):
        if i in used:
            continue
        # Find all points within tolerance
        cluster_prices = [price]
        cluster_indices = [indices[i]]
        used.add(i)

        for j in range(i + 1, len(values)):
            if j in used:
                continue
            if abs(values[j] - price) / price < price_tolerance_pct / 100:
                cluster_prices.append(values[j])
                cluster_indices.append(indices[j])
                used.add(j)

        if len(cluster_prices) >= min_touches:
            levels.append({
                "price": np.mean(cluster_prices),
                "touches": len(cluster_prices),
                "first_idx": min(cluster_indices),
                "last_idx": max(cluster_indices),
                "zone_high": max(cluster_prices),
                "zone_low": min(cluster_prices),
            })

    return levels


def find_levels(df: pd.DataFrame) -> list[dict]:
    """Find all horizontal S/R levels."""
    swing_highs, swing_lows = find_swing_points(df, order=5)

    # Combine all swing points
    all_swings = pd.concat([swing_highs, swing_lows]).sort_index()

    levels = cluster_levels(all_swings, price_tolerance_pct=1.5, min_touches=2)

    # Sort by strength (number of touches)
    levels.sort(key=lambda x: x["touches"], reverse=True)
    return levels


# ── Pattern detection ──

def detect_breakout_retest(
    df: pd.DataFrame,
    levels: list[dict],
    lookback_candles: int = 10,
    retest_window: int = 20,
) -> list[dict]:
    """Detect breakout + retest patterns.

    Logic:
    1. Price consolidates around level (at least lookback candles near it)
    2. Candle body closes beyond level (breakout)
    3. Within retest_window candles, price returns to level zone
    4. Price bounces from level (candle closes back in breakout direction)
    """
    signals = []

    for level in levels:
        lvl_price = level["price"]
        zone_width = level["zone_high"] - level["zone_low"]
        if zone_width == 0:
            zone_width = lvl_price * 0.005  # 0.5% default zone

        # Only consider level after it's been established
        start_idx = level["last_idx"] + 1

        for i in range(max(start_idx, lookback_candles), len(df) - 1):
            close = df["close"].iloc[i]
            prev_close = df["close"].iloc[i - 1]

            # ── LONG breakout: close above resistance ──
            if prev_close <= level["zone_high"] and close > level["zone_high"]:
                # Check for retest within window
                for j in range(i + 1, min(i + retest_window + 1, len(df) - 1)):
                    low_j = df["low"].iloc[j]
                    close_j = df["close"].iloc[j]

                    # Price returns to zone (retest)
                    if low_j <= level["zone_high"] * 1.005:
                        # Bounces (closes above zone)
                        if close_j > level["zone_high"]:
                            signals.append({
                                "type": "long_retest",
                                "entry_idx": j,
                                "entry_price": close_j,
                                "level_price": lvl_price,
                                "zone_high": level["zone_high"],
                                "zone_low": level["zone_low"],
                                "touches": level["touches"],
                                "breakout_idx": i,
                            })
                            break
                        # If it closes below zone, retest failed
                        elif close_j < level["zone_low"]:
                            break

            # ── SHORT breakout: close below support ──
            if prev_close >= level["zone_low"] and close < level["zone_low"]:
                for j in range(i + 1, min(i + retest_window + 1, len(df) - 1)):
                    high_j = df["high"].iloc[j]
                    close_j = df["close"].iloc[j]

                    if high_j >= level["zone_low"] * 0.995:
                        if close_j < level["zone_low"]:
                            signals.append({
                                "type": "short_retest",
                                "entry_idx": j,
                                "entry_price": close_j,
                                "level_price": lvl_price,
                                "zone_high": level["zone_high"],
                                "zone_low": level["zone_low"],
                                "touches": level["touches"],
                                "breakout_idx": i,
                            })
                            break
                        elif close_j > level["zone_high"]:
                            break

    # Deduplicate: remove signals with same entry_idx
    seen = set()
    unique = []
    for s in signals:
        if s["entry_idx"] not in seen:
            seen.add(s["entry_idx"])
            unique.append(s)

    return unique


def detect_breakout_simple(
    df: pd.DataFrame,
    levels: list[dict],
) -> list[dict]:
    """Detect simple breakout (without retest)."""
    signals = []

    for level in levels:
        start_idx = level["last_idx"] + 1
        zone_width = level["zone_high"] - level["zone_low"]
        if zone_width == 0:
            zone_width = level["price"] * 0.005

        for i in range(max(start_idx, 5), len(df) - 1):
            close = df["close"].iloc[i]
            prev_close = df["close"].iloc[i - 1]

            # LONG breakout
            if prev_close <= level["zone_high"] and close > level["zone_high"] * 1.002:
                signals.append({
                    "type": "long_breakout",
                    "entry_idx": i,
                    "entry_price": close,
                    "level_price": level["price"],
                    "zone_high": level["zone_high"],
                    "zone_low": level["zone_low"],
                    "touches": level["touches"],
                })

            # SHORT breakout
            if prev_close >= level["zone_low"] and close < level["zone_low"] * 0.998:
                signals.append({
                    "type": "short_breakout",
                    "entry_idx": i,
                    "entry_price": close,
                    "level_price": level["price"],
                    "zone_high": level["zone_high"],
                    "zone_low": level["zone_low"],
                    "touches": level["touches"],
                })

    seen = set()
    unique = []
    for s in signals:
        if s["entry_idx"] not in seen:
            seen.add(s["entry_idx"])
            unique.append(s)
    return unique


# ── Backtester ──

def backtest_signals(
    df: pd.DataFrame,
    signals: list[dict],
    rr_ratio: float = RR_RATIO,
    max_risk_pct: float = MAX_RISK_PCT,
    capital: float = INITIAL_CAPITAL,
    fee_bps: float = FEE_BPS,
) -> dict:
    """Backtest signals with fixed R:R ratio.

    SL = zone width (distance from entry to zone edge)
    TP = SL * rr_ratio
    """
    trades = []
    current_capital = capital
    in_trade = False
    trade_end_idx = 0

    for signal in sorted(signals, key=lambda s: s["entry_idx"]):
        idx = signal["entry_idx"]

        if idx <= trade_end_idx:
            continue  # skip if already in a trade

        entry_price = signal["entry_price"]
        zone_width = signal["zone_high"] - signal["zone_low"]
        if zone_width < entry_price * 0.002:
            zone_width = entry_price * 0.005  # min zone width 0.5%

        is_long = "long" in signal["type"]

        if is_long:
            sl_price = signal["zone_low"] - zone_width * 0.1  # SL just below zone
            sl_distance = entry_price - sl_price
            tp_price = entry_price + sl_distance * rr_ratio
        else:
            sl_price = signal["zone_high"] + zone_width * 0.1  # SL just above zone
            sl_distance = sl_price - entry_price
            tp_price = entry_price - sl_distance * rr_ratio

        if sl_distance <= 0 or sl_distance / entry_price > 0.15:
            continue  # invalid SL

        # Position size: risk max_risk_pct of capital
        risk_amount = current_capital * max_risk_pct / 100
        position_size = risk_amount / (sl_distance / entry_price)
        position_size = min(position_size, current_capital * 0.95)  # max 95% of capital

        # Simulate trade forward
        outcome = None
        exit_price = None
        exit_idx = None

        for j in range(idx + 1, min(idx + 200, len(df))):  # max 200 candles hold
            high = df["high"].iloc[j]
            low = df["low"].iloc[j]

            if is_long:
                if low <= sl_price:
                    outcome = "loss"
                    exit_price = sl_price
                    exit_idx = j
                    break
                if high >= tp_price:
                    outcome = "win"
                    exit_price = tp_price
                    exit_idx = j
                    break
            else:
                if high >= sl_price:
                    outcome = "loss"
                    exit_price = sl_price
                    exit_idx = j
                    break
                if low <= tp_price:
                    outcome = "win"
                    exit_price = tp_price
                    exit_idx = j
                    break

        if outcome is None:
            # Timeout: close at last price
            outcome = "timeout"
            exit_price = df["close"].iloc[min(idx + 199, len(df) - 1)]
            exit_idx = min(idx + 199, len(df) - 1)

        # Calculate PnL
        if is_long:
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price

        # Apply fees (entry + exit)
        pnl_pct -= 2 * fee_bps / 10_000

        pnl_usd = position_size * pnl_pct
        current_capital += pnl_usd
        trade_end_idx = exit_idx

        trades.append({
            "type": signal["type"],
            "entry_idx": idx,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "outcome": outcome,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "capital_after": current_capital,
            "position_size": position_size,
            "touches": signal["touches"],
            "entry_ts": df["ts"].iloc[idx],
        })

    if not trades:
        return {
            "trades": [],
            "total_return": 0,
            "win_rate": 0,
            "n_trades": 0,
            "max_dd": 0,
            "final_capital": capital,
            "sharpe": 0,
        }

    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["outcome"] == "win"]
    losses = df_trades[df_trades["outcome"] == "loss"]

    # Max drawdown on equity curve
    equity = df_trades["capital_after"].values
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = dd.min()

    # Sharpe-like on trade PnLs
    pnls = df_trades["pnl_pct"].values
    sharpe = pnls.mean() / pnls.std() * np.sqrt(len(pnls)) if pnls.std() > 0 else 0

    return {
        "trades": trades,
        "total_return": (current_capital - capital) / capital,
        "win_rate": len(wins) / len(df_trades) if len(df_trades) > 0 else 0,
        "n_trades": len(df_trades),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_timeout": len(df_trades[df_trades["outcome"] == "timeout"]),
        "max_dd": max_dd,
        "final_capital": current_capital,
        "avg_win_pct": wins["pnl_pct"].mean() if len(wins) > 0 else 0,
        "avg_loss_pct": losses["pnl_pct"].mean() if len(losses) > 0 else 0,
        "sharpe": sharpe,
    }


def run() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Miro Strategy Backtest: Breakout + Retest ===\n")
    print("Downloading data...")
    datasets = load_data()

    all_results = []

    for symbol, df in datasets.items():
        print(f"\n{'='*60}")
        print(f"  {symbol}: {len(df)} candles ({TIMEFRAME})")
        print(f"  {df['ts'].iloc[0].date()} to {df['ts'].iloc[-1].date()}")
        print(f"{'='*60}")

        # Find levels
        levels = find_levels(df)
        print(f"  Levels found: {len(levels)}")
        for lv in levels[:5]:
            print(f"    ${lv['price']:.4f} ({lv['touches']} touches, "
                  f"zone ${lv['zone_low']:.4f}-${lv['zone_high']:.4f})")

        # Strategy 1: Breakout + Retest (main strategy)
        retest_signals = detect_breakout_retest(df, levels)
        print(f"  Retest signals: {len(retest_signals)}")

        result_retest = backtest_signals(df, retest_signals)
        result_retest["symbol"] = symbol
        result_retest["strategy"] = "retest"
        all_results.append(result_retest)

        print(f"    Return: {result_retest['total_return']*100:+.1f}%, "
              f"Win rate: {result_retest['win_rate']*100:.0f}%, "
              f"Trades: {result_retest['n_trades']}, "
              f"Max DD: {result_retest['max_dd']*100:.1f}%")

        # Strategy 2: Simple breakout
        breakout_signals = detect_breakout_simple(df, levels)
        print(f"  Breakout signals: {len(breakout_signals)}")

        result_breakout = backtest_signals(df, breakout_signals)
        result_breakout["symbol"] = symbol
        result_breakout["strategy"] = "breakout"
        all_results.append(result_breakout)

        print(f"    Return: {result_breakout['total_return']*100:+.1f}%, "
              f"Win rate: {result_breakout['win_rate']*100:.0f}%, "
              f"Trades: {result_breakout['n_trades']}, "
              f"Max DD: {result_breakout['max_dd']*100:.1f}%")

    # Write report
    _write_report(all_results)


def _write_report(results: list[dict]) -> None:
    lines = [
        "# Miro Strategy Backtest: Breakout + Retest",
        "",
        f"**Timeframe:** {TIMEFRAME}",
        f"**Period:** {MONTHS_BACK} months",
        f"**R:R ratio:** 1:{RR_RATIO:.0f}",
        f"**Max risk/trade:** {MAX_RISK_PCT}% of deposit",
        f"**Initial capital:** ${INITIAL_CAPITAL:,}",
        f"**Fee:** {FEE_BPS} bps per side",
        "",
        "## Retest Strategy (main pattern)",
        "",
        "| Symbol | Trades | Wins | Losses | Win% | Return | Max DD | Final Capital |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for r in results:
        if r["strategy"] != "retest":
            continue
        lines.append(
            f"| {r['symbol']} | {r['n_trades']} | {r.get('n_wins',0)} | "
            f"{r.get('n_losses',0)} | {r['win_rate']*100:.0f}% | "
            f"{r['total_return']*100:+.1f}% | {r['max_dd']*100:.1f}% | "
            f"${r['final_capital']:,.0f} |"
        )

    lines += [
        "",
        "## Breakout Strategy (simpler pattern)",
        "",
        "| Symbol | Trades | Wins | Losses | Win% | Return | Max DD | Final Capital |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for r in results:
        if r["strategy"] != "breakout":
            continue
        lines.append(
            f"| {r['symbol']} | {r['n_trades']} | {r.get('n_wins',0)} | "
            f"{r.get('n_losses',0)} | {r['win_rate']*100:.0f}% | "
            f"{r['total_return']*100:+.1f}% | {r['max_dd']*100:.1f}% | "
            f"${r['final_capital']:,.0f} |"
        )

    # Sample trades from best/worst
    lines += [
        "",
        "## Key Observations",
        "",
        "**R:R 1:3 means:** need only 25% win rate to break even.",
        "Win rate >30% with 1:3 R:R = profitable strategy.",
        "",
        "**Level quality matters:** more touches = stronger level = better signal.",
        "",
    ]

    out = REPORTS / "miro_strategy_backtest.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    run()
