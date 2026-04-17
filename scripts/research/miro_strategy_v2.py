"""
Miro Strategy v2: улучшенный backtest.

Улучшения vs v1:
  1. Trend filter — SMA(50) на daily, торгуем только в направлении тренда
  2. Закол (false breakout) — sweep ниже уровня + возврат = доп. сигнал лонга
  3. Level quality score — touches, round numbers, zone tightness
  4. Out-of-sample split — train 4 мес, test 2 мес
  5. Multi-symbol portfolio — агрегированная equity curve

Пример:
    python scripts/research/miro_strategy_v2.py
"""
from __future__ import annotations

import sys
import time
import math
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np

DATA = Path("data/processed/candles")
REPORTS = Path("data/reports")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]
TF_ENTRY = "4h"
TF_TREND = "1d"  # daily for trend
MONTHS_BACK = 8  # 8 months: 5 train + 3 test
RR_RATIO = 3.0
MAX_RISK_PCT = 3.0  # tighter than v1
INITIAL_CAPITAL = 10_000
FEE_BPS = 10
TREND_SMA = 50  # SMA period for trend filter


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
            print(f"  Error {symbol} {timeframe}: {e}")
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


def load_data() -> dict[str, dict[str, pd.DataFrame]]:
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        result = {}
        for tf in [TF_ENTRY, TF_TREND]:
            path = DATA / f"{key}_{tf}.parquet"
            if path.exists():
                df = pd.read_parquet(path)
            else:
                sys.stdout.write(f"  Downloading {symbol} {tf}...")
                sys.stdout.flush()
                df = fetch_ohlcv(symbol, tf, MONTHS_BACK)
                if not df.empty:
                    df.to_parquet(path, index=False)
                    sys.stdout.write(f" {len(df)} candles\n")
                else:
                    sys.stdout.write(" FAILED\n")
            if not df.empty:
                result[tf] = df
        if TF_ENTRY in result:
            datasets[symbol] = result
    return datasets


# ── Trend ──

def get_trend(daily_df: pd.DataFrame, sma_period: int = TREND_SMA) -> pd.Series:
    """Return trend series aligned to daily timestamps. 1=bullish, -1=bearish, 0=neutral."""
    sma = daily_df["close"].rolling(sma_period).mean()
    trend = pd.Series(0, index=daily_df.index)
    trend[daily_df["close"] > sma] = 1
    trend[daily_df["close"] < sma] = -1
    return pd.Series(trend.values, index=daily_df["ts"])


def align_trend_to_entry(trend_series: pd.Series, entry_df: pd.DataFrame) -> pd.Series:
    """Map daily trend to each 4h candle."""
    # For each 4h candle, find the most recent daily trend
    result = pd.Series(0, index=entry_df.index)
    trend_dates = trend_series.index
    for i, row in entry_df.iterrows():
        ts = row["ts"]
        mask = trend_dates <= ts
        if mask.any():
            result.iloc[i] = trend_series[mask].iloc[-1]
    return result


# ── Level detection (improved) ──

def find_swing_points(df: pd.DataFrame, order: int = 5) -> tuple[pd.Series, pd.Series]:
    highs = pd.Series(np.nan, index=df.index)
    lows = pd.Series(np.nan, index=df.index)
    for i in range(order, len(df) - order):
        wh = df["high"].iloc[i - order:i + order + 1]
        if df["high"].iloc[i] == wh.max():
            highs.iloc[i] = df["high"].iloc[i]
        wl = df["low"].iloc[i - order:i + order + 1]
        if df["low"].iloc[i] == wl.min():
            lows.iloc[i] = df["low"].iloc[i]
    return highs.dropna(), lows.dropna()


def is_round_number(price: float) -> bool:
    """Check if price is near a round number."""
    if price == 0:
        return False
    # Determine significance based on magnitude
    magnitude = 10 ** int(math.log10(abs(price)))
    # Check if close to multiple of magnitude or magnitude/10
    for divisor in [magnitude, magnitude / 2, magnitude / 5, magnitude / 10]:
        if divisor > 0:
            remainder = price % divisor
            if remainder / divisor < 0.02 or remainder / divisor > 0.98:
                return True
    return False


def cluster_levels(
    swing_points: pd.Series,
    price_tolerance_pct: float = 1.0,
    min_touches: int = 2,
) -> list[dict]:
    if swing_points.empty:
        return []
    values = swing_points.sort_values().values
    indices = swing_points.sort_values().index.tolist()
    levels = []
    used = set()

    for i, price in enumerate(values):
        if i in used:
            continue
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
            mean_price = np.mean(cluster_prices)
            zone_h = max(cluster_prices)
            zone_l = min(cluster_prices)
            zone_width_pct = (zone_h - zone_l) / mean_price * 100

            # Quality score
            score = len(cluster_prices)  # base = touches
            if is_round_number(mean_price):
                score += 2  # round number bonus
            if zone_width_pct < 0.5:
                score += 1  # tight zone bonus
            if len(cluster_prices) >= 4:
                score += 2  # many touches bonus

            levels.append({
                "price": mean_price,
                "touches": len(cluster_prices),
                "first_idx": min(cluster_indices),
                "last_idx": max(cluster_indices),
                "zone_high": zone_h,
                "zone_low": zone_l,
                "zone_width_pct": zone_width_pct,
                "is_round": is_round_number(mean_price),
                "score": score,
            })

    levels.sort(key=lambda x: x["score"], reverse=True)
    return levels


def find_levels(df: pd.DataFrame) -> list[dict]:
    swing_highs, swing_lows = find_swing_points(df, order=5)
    all_swings = pd.concat([swing_highs, swing_lows]).sort_index()
    return cluster_levels(all_swings, price_tolerance_pct=1.0, min_touches=2)


# ── Pattern detection (improved) ──

def detect_signals(
    df: pd.DataFrame,
    levels: list[dict],
    trend: pd.Series,
    use_trend_filter: bool = True,
    retest_window: int = 20,
    min_level_score: int = 3,
) -> list[dict]:
    """Detect breakout+retest and закол signals with trend filter."""
    signals = []

    for level in levels:
        if level["score"] < min_level_score:
            continue

        lvl = level["price"]
        zh = level["zone_high"]
        zl = level["zone_low"]
        zone_w = zh - zl
        if zone_w == 0:
            zone_w = lvl * 0.005

        start_idx = level["last_idx"] + 1

        for i in range(max(start_idx, 10), len(df) - 1):
            close_i = df["close"].iloc[i]
            prev_close = df["close"].iloc[i - 1]
            low_i = df["low"].iloc[i]
            high_i = df["high"].iloc[i]

            candle_trend = trend.iloc[i] if i < len(trend) else 0

            # ── LONG: breakout above resistance ──
            if prev_close <= zh and close_i > zh * 1.001:
                if use_trend_filter and candle_trend == -1:
                    continue  # skip longs in downtrend

                # Look for retest
                for j in range(i + 1, min(i + retest_window + 1, len(df) - 1)):
                    low_j = df["low"].iloc[j]
                    close_j = df["close"].iloc[j]

                    if low_j <= zh * 1.005:
                        if close_j > zh:
                            signals.append({
                                "type": "long_retest",
                                "entry_idx": j,
                                "entry_price": close_j,
                                "level": level,
                                "trend": candle_trend,
                            })
                            break
                        elif close_j < zl:
                            break

            # ── SHORT: breakout below support ──
            if prev_close >= zl and close_i < zl * 0.999:
                if use_trend_filter and candle_trend == 1:
                    continue  # skip shorts in uptrend

                for j in range(i + 1, min(i + retest_window + 1, len(df) - 1)):
                    high_j = df["high"].iloc[j]
                    close_j = df["close"].iloc[j]

                    if high_j >= zl * 0.995:
                        if close_j < zl:
                            signals.append({
                                "type": "short_retest",
                                "entry_idx": j,
                                "entry_price": close_j,
                                "level": level,
                                "trend": candle_trend,
                            })
                            break
                        elif close_j > zh:
                            break

            # ── LONG: закол (false breakout below support) + recovery ──
            # Price sweeps below support but closes back above
            if low_i < zl and close_i > zl:
                if use_trend_filter and candle_trend == -1:
                    continue
                # Закол confirmed — enter long on next candle that closes above zone
                for j in range(i + 1, min(i + 5, len(df) - 1)):
                    if df["close"].iloc[j] > zh:
                        signals.append({
                            "type": "long_zakol",
                            "entry_idx": j,
                            "entry_price": df["close"].iloc[j],
                            "level": level,
                            "trend": candle_trend,
                        })
                        break

    # Deduplicate
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
    trades = []
    current_capital = capital
    trade_end_idx = 0

    for signal in sorted(signals, key=lambda s: s["entry_idx"]):
        idx = signal["entry_idx"]
        if idx <= trade_end_idx:
            continue

        entry_price = signal["entry_price"]
        level = signal["level"]
        zh = level["zone_high"]
        zl = level["zone_low"]
        zone_w = zh - zl
        if zone_w < entry_price * 0.003:
            zone_w = entry_price * 0.005

        is_long = "long" in signal["type"]

        if is_long:
            sl_price = zl - zone_w * 0.2
            sl_dist = entry_price - sl_price
            tp_price = entry_price + sl_dist * rr_ratio
        else:
            sl_price = zh + zone_w * 0.2
            sl_dist = sl_price - entry_price
            tp_price = entry_price - sl_dist * rr_ratio

        if sl_dist <= 0 or sl_dist / entry_price > 0.10:
            continue

        risk_amount = current_capital * max_risk_pct / 100
        position_size = risk_amount / (sl_dist / entry_price)
        position_size = min(position_size, current_capital * 0.90)

        outcome = None
        exit_price = None
        exit_idx = None

        for j in range(idx + 1, min(idx + 150, len(df))):
            h = df["high"].iloc[j]
            l = df["low"].iloc[j]
            if is_long:
                if l <= sl_price:
                    outcome, exit_price, exit_idx = "loss", sl_price, j
                    break
                if h >= tp_price:
                    outcome, exit_price, exit_idx = "win", tp_price, j
                    break
            else:
                if h >= sl_price:
                    outcome, exit_price, exit_idx = "loss", sl_price, j
                    break
                if l <= tp_price:
                    outcome, exit_price, exit_idx = "win", tp_price, j
                    break

        if outcome is None:
            outcome = "timeout"
            exit_idx = min(idx + 149, len(df) - 1)
            exit_price = df["close"].iloc[exit_idx]

        pnl_pct = (exit_price - entry_price) / entry_price if is_long else (entry_price - exit_price) / entry_price
        pnl_pct -= 2 * fee_bps / 10_000
        pnl_usd = position_size * pnl_pct
        current_capital += pnl_usd
        trade_end_idx = exit_idx

        trades.append({
            "type": signal["type"],
            "entry_idx": idx,
            "entry_ts": df["ts"].iloc[idx],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "outcome": outcome,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "capital_after": current_capital,
            "level_score": level["score"],
            "level_touches": level["touches"],
            "is_round": level["is_round"],
        })

    if not trades:
        return {"trades": [], "n_trades": 0, "total_return": 0, "win_rate": 0,
                "max_dd": 0, "final_capital": capital, "sharpe": 0,
                "n_wins": 0, "n_losses": 0, "n_timeout": 0,
                "avg_win_pct": 0, "avg_loss_pct": 0, "profit_factor": 0}

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["outcome"] == "win"]
    losses = tdf[tdf["outcome"] == "loss"]

    equity = tdf["capital_after"].values
    peak = np.maximum.accumulate(np.concatenate([[capital], equity]))
    dd = (np.concatenate([[capital], equity]) - peak) / peak
    max_dd = dd.min()

    pnls = tdf["pnl_pct"].values
    sharpe = pnls.mean() / pnls.std() * np.sqrt(len(pnls)) if pnls.std() > 0 else 0

    gross_win = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    return {
        "trades": trades,
        "n_trades": len(tdf),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_timeout": len(tdf[tdf["outcome"] == "timeout"]),
        "total_return": (current_capital - capital) / capital,
        "win_rate": len(wins) / len(tdf),
        "max_dd": max_dd,
        "final_capital": current_capital,
        "sharpe": sharpe,
        "avg_win_pct": wins["pnl_pct"].mean() if len(wins) > 0 else 0,
        "avg_loss_pct": losses["pnl_pct"].mean() if len(losses) > 0 else 0,
        "profit_factor": profit_factor,
    }


def run() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Miro Strategy v2: Improved Backtest ===\n")
    print("Downloading data...")
    datasets = load_data()

    # ── Run variants ──
    variants = {
        "v2_trend+retest":       {"trend": True, "min_score": 3},
        "v2_no_trend":           {"trend": False, "min_score": 3},
        "v2_trend+high_quality": {"trend": True, "min_score": 5},
    }

    all_results = []
    portfolio_trades = {v: [] for v in variants}

    for symbol, data in datasets.items():
        df = data[TF_ENTRY]

        # Split: train (first 5/8) / test (last 3/8)
        split_idx = int(len(df) * 5 / 8)
        df_train = df.iloc[:split_idx].reset_index(drop=True)
        df_test = df.iloc[split_idx:].reset_index(drop=True)

        # Trend from daily
        if TF_TREND in data:
            trend_daily = get_trend(data[TF_TREND], TREND_SMA)
            trend_train = align_trend_to_entry(trend_daily, df_train)
            trend_test = align_trend_to_entry(trend_daily, df_test)
        else:
            trend_train = pd.Series(0, index=df_train.index)
            trend_test = pd.Series(0, index=df_test.index)

        # Find levels on TRAIN data only (no look-ahead)
        levels = find_levels(df_train)

        # Adjust level indices for test set (keep levels, but they're from train period)
        # Levels are "known" — we detect signals on test data using known levels

        print(f"\n{'='*60}")
        print(f"  {symbol}: train={len(df_train)}, test={len(df_test)} candles ({TF_ENTRY})")
        print(f"  Train: {df_train['ts'].iloc[0].date()} to {df_train['ts'].iloc[-1].date()}")
        print(f"  Test:  {df_test['ts'].iloc[0].date()} to {df_test['ts'].iloc[-1].date()}")
        print(f"  Levels: {len(levels)} (top score: {levels[0]['score'] if levels else 0})")

        for variant_name, cfg in variants.items():
            # ── TRAIN ──
            signals_train = detect_signals(
                df_train, levels, trend_train,
                use_trend_filter=cfg["trend"],
                min_level_score=cfg["min_score"],
            )
            result_train = backtest_signals(df_train, signals_train)

            # ── TEST (out-of-sample) ──
            # For test: use levels found in train, detect new signals in test data
            # Need to reset level indices to work with test df
            test_levels = []
            for lv in levels:
                # Only include levels whose price is still relevant
                # (price within 50% of test data range)
                test_range = df_test["close"].max() - df_test["close"].min()
                test_mid = (df_test["close"].max() + df_test["close"].min()) / 2
                if abs(lv["price"] - test_mid) < test_range:
                    lv_copy = dict(lv)
                    lv_copy["first_idx"] = 0
                    lv_copy["last_idx"] = 0  # already established
                    test_levels.append(lv_copy)

            signals_test = detect_signals(
                df_test, test_levels, trend_test,
                use_trend_filter=cfg["trend"],
                min_level_score=cfg["min_score"],
            )
            result_test = backtest_signals(df_test, signals_test)

            r = {
                "symbol": symbol,
                "variant": variant_name,
                "train_return": result_train["total_return"],
                "train_wr": result_train["win_rate"],
                "train_trades": result_train["n_trades"],
                "train_dd": result_train["max_dd"],
                "train_pf": result_train["profit_factor"],
                "test_return": result_test["total_return"],
                "test_wr": result_test["win_rate"],
                "test_trades": result_test["n_trades"],
                "test_dd": result_test["max_dd"],
                "test_pf": result_test["profit_factor"],
                "test_sharpe": result_test["sharpe"],
                "test_final": result_test["final_capital"],
            }
            all_results.append(r)

            # Collect trades for portfolio
            for t in result_test["trades"]:
                t["symbol"] = symbol
                t["variant"] = variant_name
                portfolio_trades[variant_name].append(t)

            print(f"  [{variant_name}] train: {result_train['n_trades']}t "
                  f"{result_train['win_rate']*100:.0f}%WR {result_train['total_return']*100:+.1f}% | "
                  f"test: {result_test['n_trades']}t "
                  f"{result_test['win_rate']*100:.0f}%WR {result_test['total_return']*100:+.1f}%")

    # ── Portfolio analysis ──
    print(f"\n{'='*60}")
    print("  PORTFOLIO ANALYSIS (all symbols combined)")
    print(f"{'='*60}")

    for variant_name, trades in portfolio_trades.items():
        if not trades:
            print(f"\n  [{variant_name}] No trades")
            continue

        tdf = pd.DataFrame(trades)
        wins = tdf[tdf["outcome"] == "win"]
        losses = tdf[tdf["outcome"] == "loss"]
        n = len(tdf)
        wr = len(wins) / n if n > 0 else 0
        avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
        gross_win = wins["pnl_usd"].sum() if len(wins) > 0 else 0
        gross_loss = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
        pf = gross_win / gross_loss if gross_loss > 0 else 0

        # Expectancy per trade
        expectancy_pct = wr * avg_win - (1 - wr) * avg_loss

        print(f"\n  [{variant_name}]")
        print(f"    Total trades: {n}")
        print(f"    Wins: {len(wins)}, Losses: {len(losses)}, Timeout: {len(tdf[tdf['outcome']=='timeout'])}")
        print(f"    Win rate: {wr*100:.1f}%")
        print(f"    Avg win: {avg_win*100:+.2f}%, Avg loss: {avg_loss*100:.2f}%")
        print(f"    Profit factor: {pf:.2f}")
        print(f"    Expectancy/trade: {expectancy_pct*100:+.3f}%")

        # ── Доходность estimation ──
        # Assume we trade 8 symbols, each gets ~test_trades / test_months trades per month
        test_months = 3
        trades_per_month = n / test_months
        monthly_expectancy = trades_per_month * expectancy_pct * MAX_RISK_PCT / 100
        annual_return = (1 + monthly_expectancy) ** 12 - 1

        print(f"\n    === RETURN ESTIMATION (out-of-sample) ===")
        print(f"    Trades/month (all symbols): {trades_per_month:.1f}")
        print(f"    Expectancy/trade: {expectancy_pct*100:+.3f}% of position")
        print(f"    Risk/trade: {MAX_RISK_PCT}% of capital")
        print(f"    Monthly return: {monthly_expectancy*100:+.2f}%")
        print(f"    Annual return (compounded): {annual_return*100:+.1f}%")

        # Scenarios
        print(f"\n    Scenarios (annual, compounded):")
        for scenario, wr_adj, note in [
            ("pessimistic", -0.05, "WR -5pp vs backtest"),
            ("base", 0, "as backtest"),
            ("optimistic", +0.05, "WR +5pp (trend filter + exp)"),
        ]:
            adj_wr = wr + wr_adj
            adj_exp = adj_wr * avg_win - (1 - adj_wr) * avg_loss
            adj_monthly = trades_per_month * adj_exp * MAX_RISK_PCT / 100
            adj_annual = (1 + adj_monthly) ** 12 - 1
            print(f"      {scenario:12s}: WR={adj_wr*100:.0f}%, "
                  f"monthly={adj_monthly*100:+.2f}%, "
                  f"annual={adj_annual*100:+.1f}% ({note})")

    # ── Write report ──
    _write_report(all_results, portfolio_trades)


def _write_report(results: list[dict], portfolio_trades: dict) -> None:
    rdf = pd.DataFrame(results)

    lines = [
        "# Miro Strategy v2: Improved Backtest",
        "",
        f"**Entry TF:** {TF_ENTRY}, **Trend TF:** {TF_TREND} SMA({TREND_SMA})",
        f"**R:R:** 1:{RR_RATIO:.0f}, **Risk/trade:** {MAX_RISK_PCT}%",
        f"**Symbols:** {len(SYMBOLS)}, **Period:** {MONTHS_BACK} months (5 train + 3 test)",
        f"**Capital:** ${INITIAL_CAPITAL:,}, **Fee:** {FEE_BPS} bps/side",
        "",
        "## Out-of-Sample Results (test period only)",
        "",
    ]

    for variant in rdf["variant"].unique():
        vdf = rdf[rdf["variant"] == variant]
        lines += [
            f"### {variant}",
            "",
            "| Symbol | Train trades | Train WR | Train return | Test trades | Test WR | Test return | Test DD | PF |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for _, r in vdf.iterrows():
            lines.append(
                f"| {r['symbol']} | {r['train_trades']} | "
                f"{r['train_wr']*100:.0f}% | {r['train_return']*100:+.1f}% | "
                f"{r['test_trades']} | {r['test_wr']*100:.0f}% | "
                f"{r['test_return']*100:+.1f}% | {r['test_dd']*100:.1f}% | "
                f"{r['test_pf']:.2f} |"
            )

        # Portfolio summary
        trades = portfolio_trades.get(variant, [])
        if trades:
            tdf = pd.DataFrame(trades)
            wins = tdf[tdf["outcome"] == "win"]
            n = len(tdf)
            wr = len(wins) / n if n > 0 else 0
            avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
            avg_loss = abs(tdf[tdf["outcome"]=="loss"]["pnl_pct"].mean()) if len(tdf[tdf["outcome"]=="loss"]) > 0 else 0
            expectancy = wr * avg_win - (1 - wr) * avg_loss
            monthly = n / 3 * expectancy * MAX_RISK_PCT / 100
            annual = (1 + monthly) ** 12 - 1

            lines += [
                "",
                f"**Portfolio ({variant}):** {n} trades, {wr*100:.0f}% WR, "
                f"expectancy {expectancy*100:+.3f}%/trade, "
                f"monthly {monthly*100:+.2f}%, annual **{annual*100:+.1f}%**",
                "",
            ]

    lines += [
        "## Methodology Notes",
        "",
        "- Levels found on TRAIN data only (no look-ahead bias)",
        "- Test period uses pre-established levels to detect new signals",
        "- Trend filter uses daily SMA(50) — only trade in trend direction",
        "- Закол (false breakout): sweep below support + recovery = long signal",
        "- Level quality score: touches + round number bonus + zone tightness",
        "",
    ]

    out = REPORTS / "miro_strategy_v2.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    run()
