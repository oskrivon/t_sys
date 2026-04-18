"""
Backtest Miro Strategy on 1H timeframe.

Compare with 4H baseline to evaluate shorter-term applicability.
Tests multiple parameter sets: standard, tight (scalp-like), aggressive.

Usage:
    python scripts/research/backtest_1h.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import ccxt

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.models import Breakout

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10
INITIAL_CAPITAL = 10_000


@dataclass
class Config:
    name: str
    tf: str
    months: int
    level_lookback: int
    level_min_touches: int
    level_tolerance_pct: float
    retest_window: int
    min_level_age: int
    trend_sma: int
    rr_ratio: float
    max_risk_pct: float
    check_interval: int
    max_hold: int  # max candles to hold


# Parameter sets to test
CONFIGS = [
    Config(
        name="1H_standard",
        tf="1h", months=4,
        level_lookback=200, level_min_touches=2, level_tolerance_pct=1.0,
        retest_window=15, min_level_age=20, trend_sma=50,
        rr_ratio=3.0, max_risk_pct=3.0, check_interval=4, max_hold=150,
    ),
    Config(
        name="1H_tight",
        tf="1h", months=4,
        level_lookback=120, level_min_touches=2, level_tolerance_pct=0.7,
        retest_window=10, min_level_age=12, trend_sma=30,
        rr_ratio=2.5, max_risk_pct=2.0, check_interval=3, max_hold=72,
    ),
    Config(
        name="1H_scalp",
        tf="1h", months=4,
        level_lookback=72, level_min_touches=2, level_tolerance_pct=0.5,
        retest_window=6, min_level_age=6, trend_sma=20,
        rr_ratio=2.0, max_risk_pct=2.0, check_interval=2, max_hold=24,
    ),
    Config(
        name="4H_baseline",
        tf="4h", months=8,
        level_lookback=200, level_min_touches=2, level_tolerance_pct=1.0,
        retest_window=15, min_level_age=20, trend_sma=50,
        rr_ratio=3.0, max_risk_pct=3.0, check_interval=6, max_hold=150,
    ),
]


def fetch_ohlcv(symbol: str, tf: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c = []
    cur = since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except Exception:
            break
        if not c:
            break
        all_c.extend(c)
        last = c[-1][0]
        if last <= cur or len(c) < 1000:
            break
        cur = last + 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def load_data(tf: str, months: int) -> dict[str, pd.DataFrame]:
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{tf}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            # Check if enough data
            if len(df) >= months * 30 * (24 if tf == "1h" else 6) * 0.5:
                datasets[symbol] = df
                continue
        sys.stdout.write(f"  Downloading {symbol} {tf} ({months}m)...")
        sys.stdout.flush()
        df = fetch_ohlcv(symbol, tf, months)
        if not df.empty:
            df.to_parquet(path, index=False)
            sys.stdout.write(f" {len(df)} candles\n")
            datasets[symbol] = df
        else:
            sys.stdout.write(" FAILED\n")
    return datasets


def run_backtest(df: pd.DataFrame, symbol: str, cfg: Config) -> dict:
    """Walk-forward backtest with given config."""
    sma = df["close"].rolling(cfg.trend_sma).mean()
    trend = pd.Series(0, index=df.index)
    trend[df["close"] > sma] = 1
    trend[df["close"] < sma] = -1

    capital = INITIAL_CAPITAL
    trades = []
    active_trade = None
    recent_breakouts: list[Breakout] = []
    cached_levels = []
    last_check = 0

    warmup = max(cfg.level_lookback, cfg.trend_sma) + 10

    for i in range(warmup, len(df)):
        # -- Manage active trade --
        if active_trade is not None:
            h, l = df["high"].iloc[i], df["low"].iloc[i]
            at = active_trade
            outcome = None
            exit_price = None

            if at["is_long"]:
                if l <= at["sl"]:
                    outcome, exit_price = "loss", at["sl"]
                elif h >= at["tp"]:
                    outcome, exit_price = "win", at["tp"]
                elif i - at["entry_idx"] > cfg.max_hold:
                    outcome, exit_price = "timeout", df["close"].iloc[i]
            else:
                if h >= at["sl"]:
                    outcome, exit_price = "loss", at["sl"]
                elif l <= at["tp"]:
                    outcome, exit_price = "win", at["tp"]
                elif i - at["entry_idx"] > cfg.max_hold:
                    outcome, exit_price = "timeout", df["close"].iloc[i]

            if outcome:
                ep = at["entry_price"]
                if at["is_long"]:
                    pnl_pct = (exit_price - ep) / ep - 2 * FEE_BPS / 10000
                else:
                    pnl_pct = (ep - exit_price) / ep - 2 * FEE_BPS / 10000
                pnl_usd = at["position_size"] * pnl_pct
                capital += pnl_usd
                trades.append({
                    "outcome": outcome, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                    "capital_after": capital, "type": at["type"],
                    "entry_ts": df["ts"].iloc[at["entry_idx"]],
                    "exit_ts": df["ts"].iloc[i],
                    "hold_candles": i - at["entry_idx"],
                })
                active_trade = None
            else:
                continue

        # -- Update levels --
        if i - last_check >= cfg.check_interval:
            cached_levels = get_rolling_levels(
                df, i, cfg.level_lookback, cfg.level_min_touches,
                cfg.level_tolerance_pct, cfg.min_level_age,
            )
            last_check = i

            new_brk = detect_breakouts(df, i, cached_levels)
            recent_breakouts.extend(new_brk)
            recent_breakouts = [b for b in recent_breakouts if i - b.idx <= cfg.retest_window]

        # -- Find signals --
        close_i = df["close"].iloc[i]

        retest = detect_retests(df, i, recent_breakouts, cfg.retest_window, trend)
        zakol = detect_zakol(df, i, cached_levels, trend)

        best = None
        if retest and zakol:
            best = retest if retest[2] >= zakol[2] else zakol
        else:
            best = retest or zakol

        if best and capital > 100:
            sig_type, lv, _ = best
            signal = build_signal(symbol, sig_type, lv, close_i, cfg.rr_ratio)
            if signal is None:
                continue

            sl_dist = abs(close_i - signal.sl)
            risk_amt = capital * cfg.max_risk_pct / 100
            pos_size = risk_amt / (sl_dist / close_i)
            pos_size = min(pos_size, capital * 0.90)

            active_trade = {
                "entry_idx": i, "entry_price": close_i,
                "sl": signal.sl, "tp": signal.tp,
                "is_long": signal.is_long,
                "position_size": pos_size,
                "type": sig_type.value,
            }

    if not trades:
        return {"symbol": symbol, "n_trades": 0, "win_rate": 0, "total_return": 0,
                "max_dd": 0, "profit_factor": 0, "avg_hold": 0, "trades": []}

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["outcome"] == "win"]
    losses = tdf[tdf["outcome"] == "loss"]

    eq = np.concatenate([[INITIAL_CAPITAL], tdf["capital_after"].values])
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = dd.min()

    gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    pf = gw / gl if gl > 0 else 0
    wr = len(wins) / len(tdf)

    return {
        "symbol": symbol,
        "n_trades": len(tdf),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_timeout": len(tdf[tdf["outcome"] == "timeout"]),
        "win_rate": wr,
        "total_return": (tdf["capital_after"].iloc[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL,
        "max_dd": max_dd,
        "profit_factor": pf,
        "avg_hold": tdf["hold_candles"].mean(),
        "trades": trades,
    }


def run_config(cfg: Config, datasets: dict[str, pd.DataFrame]) -> None:
    """Run backtest for one config across all symbols."""
    print(f"\n{'='*70}")
    print(f"  {cfg.name}: TF={cfg.tf}, lookback={cfg.level_lookback}, "
          f"retest_window={cfg.retest_window}, R:R=1:{cfg.rr_ratio}, "
          f"max_hold={cfg.max_hold}")
    print(f"{'='*70}")

    all_results = []
    all_trades = []

    for symbol, df in datasets.items():
        result = run_backtest(df, symbol, cfg)
        all_results.append(result)
        for t in result["trades"]:
            t["symbol"] = symbol
            all_trades.append(t)

        if result["n_trades"] > 0:
            print(f"  {symbol:12s}: {result['n_trades']:3d}t, "
                  f"{result['win_rate']*100:.0f}% WR, "
                  f"{result['total_return']*100:+.1f}%, "
                  f"PF {result['profit_factor']:.2f}, "
                  f"hold {result['avg_hold']:.0f}c")

    if not all_trades:
        print("  No trades!")
        return

    tdf = pd.DataFrame(all_trades)
    wins = tdf[tdf["outcome"] == "win"]
    losses = tdf[tdf["outcome"] == "loss"]
    n = len(tdf)
    wr = len(wins) / n
    aw = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    al = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
    exp = wr * aw - (1 - wr) * al
    gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    pf = gw / gl if gl > 0 else 0

    date_range = (tdf["exit_ts"].max() - tdf["entry_ts"].min()).total_seconds() / (30 * 86400)
    tpm = n / date_range if date_range > 0 else 0
    avg_hold = tdf["hold_candles"].mean()

    # Hours per candle
    hours_per_candle = 1 if cfg.tf == "1h" else 4 if cfg.tf == "4h" else 1
    avg_hold_hours = avg_hold * hours_per_candle

    monthly_ret = tpm * exp * cfg.max_risk_pct / 100
    annual_ret = (1 + monthly_ret) ** 12 - 1

    print(f"\n  PORTFOLIO: {n} trades ({tpm:.1f}/mo), WR {wr*100:.1f}%, PF {pf:.2f}")
    print(f"  Avg win: {aw*100:+.2f}%, Avg loss: -{al*100:.2f}%, Exp: {exp*100:+.3f}%")
    print(f"  Avg hold: {avg_hold:.0f} candles = {avg_hold_hours:.0f} hours")
    print(f"  Monthly: {monthly_ret*100:+.2f}%, Annual: {annual_ret*100:+.1f}%")

    # By signal type
    print(f"\n  By type:")
    for stype in tdf["type"].unique():
        sub = tdf[tdf["type"] == stype]
        sw = sub[sub["outcome"] == "win"]
        print(f"    {stype:18s}: {len(sub):3d}t, {len(sw)/len(sub)*100:.0f}% WR")

    return {
        "name": cfg.name,
        "trades": n, "tpm": tpm, "wr": wr, "pf": pf,
        "exp": exp, "monthly": monthly_ret, "annual": annual_ret,
        "avg_hold_hours": avg_hold_hours,
    }


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Miro Strategy: Multi-TF Backtest ===")

    # Load data for each TF
    datasets_by_tf: dict[str, dict[str, pd.DataFrame]] = {}
    for cfg in CONFIGS:
        if cfg.tf not in datasets_by_tf:
            print(f"\nLoading {cfg.tf} data...")
            datasets_by_tf[cfg.tf] = load_data(cfg.tf, cfg.months)
            print(f"  Loaded {len(datasets_by_tf[cfg.tf])} symbols")

    # Run each config
    summaries = []
    for cfg in CONFIGS:
        result = run_config(cfg, datasets_by_tf[cfg.tf])
        if result:
            summaries.append(result)

    # Comparison table
    if summaries:
        print(f"\n{'='*70}")
        print(f"  COMPARISON")
        print(f"{'='*70}")
        print(f"  {'Config':<16} {'Trades':>7} {'T/mo':>6} {'WR':>6} {'PF':>6} "
              f"{'Exp':>8} {'Monthly':>8} {'Annual':>8} {'Hold(h)':>8}")
        for s in summaries:
            print(f"  {s['name']:<16} {s['trades']:>7} {s['tpm']:>5.1f} "
                  f"{s['wr']*100:>5.1f}% {s['pf']:>5.2f} "
                  f"{s['exp']*100:>+7.3f}% {s['monthly']*100:>+7.2f}% "
                  f"{s['annual']*100:>+7.1f}% {s['avg_hold_hours']:>7.0f}")


if __name__ == "__main__":
    main()
