"""Pairs Trading / Statistical Arbitrage Backtest.

Logic:
  1. For each pair (A, B): compute price ratio = A/B
  2. Rolling z-score of ratio (lookback window)
  3. When z > threshold: ratio too high → short A, long B (expect reversion)
  4. When z < -threshold: ratio too low → long A, short B
  5. Exit when z crosses zero (mean reversion) or timeout
  6. Market neutral: equal dollar notional on each leg

Split: first 50% = TRAIN, second 50% = TEST.
Scan all possible pairs, rank by Sharpe on TRAIN, validate top pairs on TEST.
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backtest.presets import bybit_futures

DATA_DIR = Path("data/processed/candles")
TIMEFRAME = "4h"

# Core symbols (high liquidity)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ARBUSDT", "SUIUSDT",
    "ADAUSDT", "DOTUSDT", "APTUSDT", "NEARUSDT", "OPUSDT",
    "ATOMUSDT", "FILUSDT", "MATICUSDT", "LTCUSDT", "UNIUSDT",
]

# Strategy params
LOOKBACK = 80           # rolling window for z-score (~13 days on 4h)
Z_ENTRY = 2.0           # enter when |z| > this
Z_EXIT = 0.0            # exit when z crosses this (mean)
Z_STOP = 4.0            # stop loss: z diverges further
MAX_HOLD = 120          # max hold candles (~20 days)
POSITION_SIZE = 500     # per leg (total = 2x)

# Fee model: 2 legs x entry + exit = 4 orders
FEE_PER_LEG_BPS = 7.0   # taker 5.5 + slippage 1.5


def load_data() -> dict[str, pd.DataFrame]:
    data = {}
    for sym in SYMBOLS:
        path = DATA_DIR / f"{sym}_{TIMEFRAME}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            if len(df) >= 500:
                data[sym] = df
    return data


def align_pair(df_a: pd.DataFrame, df_b: pd.DataFrame):
    """Align two DataFrames by timestamp, return aligned closes."""
    merged = pd.merge(
        df_a[["ts", "close"]].rename(columns={"close": "a"}),
        df_b[["ts", "close"]].rename(columns={"close": "b"}),
        on="ts", how="inner",
    )
    return merged


def backtest_pair(
    merged: pd.DataFrame,
    z_entry: float = Z_ENTRY,
    z_exit: float = Z_EXIT,
    z_stop: float = Z_STOP,
    lookback: int = LOOKBACK,
    max_hold: int = MAX_HOLD,
) -> list[dict]:
    """Backtest a single pair. Returns list of trade dicts."""
    ratio = merged["a"] / merged["b"]
    ratio_mean = ratio.rolling(lookback).mean()
    ratio_std = ratio.rolling(lookback).std()
    z = (ratio - ratio_mean) / ratio_std.replace(0, 1e-10)

    trades = []
    position = 0  # +1 = long ratio (long A, short B), -1 = short ratio
    entry_idx = 0
    entry_z = 0

    for i in range(lookback, len(merged)):
        zi = z.iloc[i]
        if np.isnan(zi):
            continue

        if position == 0:
            # Entry
            if zi > z_entry:
                position = -1  # short ratio: short A, long B
                entry_idx = i
                entry_z = zi
            elif zi < -z_entry:
                position = 1   # long ratio: long A, short B
                entry_idx = i
                entry_z = zi

        else:
            # Exit conditions
            hold = i - entry_idx
            exit_reason = None

            if position == 1 and zi >= z_exit:
                exit_reason = "mean_reversion"
            elif position == -1 and zi <= z_exit:
                exit_reason = "mean_reversion"
            elif position == 1 and zi < -z_stop:
                exit_reason = "stop"
            elif position == -1 and zi > z_stop:
                exit_reason = "stop"
            elif hold >= max_hold:
                exit_reason = "timeout"

            if exit_reason:
                # Compute PnL
                entry_a = merged["a"].iloc[entry_idx]
                exit_a = merged["a"].iloc[i]
                entry_b = merged["b"].iloc[entry_idx]
                exit_b = merged["b"].iloc[i]

                if position == 1:  # long A, short B
                    pnl_a = (exit_a - entry_a) / entry_a  # long leg
                    pnl_b = (entry_b - exit_b) / entry_b  # short leg
                else:  # short A, long B
                    pnl_a = (entry_a - exit_a) / entry_a  # short leg
                    pnl_b = (exit_b - entry_b) / entry_b  # long leg

                gross_pnl_pct = (pnl_a + pnl_b) / 2 * 100  # avg of two legs
                fees_pct = FEE_PER_LEG_BPS * 2 * 2 / 10000 * 100  # 4 orders
                net_pnl_pct = gross_pnl_pct - fees_pct

                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_time": merged["ts"].iloc[entry_idx],
                    "exit_time": merged["ts"].iloc[i],
                    "position": position,
                    "entry_z": entry_z,
                    "exit_z": zi,
                    "gross_pnl_pct": gross_pnl_pct,
                    "net_pnl_pct": net_pnl_pct,
                    "hold_candles": hold,
                    "exit_reason": exit_reason,
                    "pnl_a_pct": pnl_a * 100,
                    "pnl_b_pct": pnl_b * 100,
                })

                position = 0

    return trades


def compute_pair_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    df = pd.DataFrame(trades)
    n = len(df)
    wins = (df["net_pnl_pct"] > 0).sum()
    wr = wins / n
    avg_pnl = df["net_pnl_pct"].mean()
    total_pnl = df["net_pnl_pct"].sum()
    avg_hold = df["hold_candles"].mean()

    # Sharpe (annualized)
    returns = df["net_pnl_pct"].values / 100
    if returns.std() > 0:
        days = (df["exit_time"].iloc[-1] - df["entry_time"].iloc[0]).days
        trades_per_year = n / max(days, 1) * 365
        sharpe = returns.mean() / returns.std() * np.sqrt(trades_per_year)
    else:
        sharpe = 0

    # PF
    gross_wins = df[df["net_pnl_pct"] > 0]["net_pnl_pct"].sum()
    gross_losses = abs(df[df["net_pnl_pct"] <= 0]["net_pnl_pct"].sum())
    pf = gross_wins / gross_losses if gross_losses > 0 else 999

    # Exit reasons
    reasons = df["exit_reason"].value_counts().to_dict()

    return {
        "n": n, "wr": wr, "avg_pnl": avg_pnl, "total_pnl": total_pnl,
        "sharpe": sharpe, "pf": pf, "avg_hold": avg_hold, "reasons": reasons,
    }


def main():
    print("Loading data...")
    data = load_data()
    print(f"Loaded {len(data)} symbols")

    # Generate all pairs
    symbols = list(data.keys())
    pairs = list(combinations(symbols, 2))
    print(f"Testing {len(pairs)} pairs...")

    # Split
    split_results = []

    for sym_a, sym_b in pairs:
        merged = align_pair(data[sym_a], data[sym_b])
        if len(merged) < LOOKBACK * 3:
            continue

        mid = len(merged) // 2
        train = merged.iloc[:mid].reset_index(drop=True)
        test = merged.iloc[mid:].reset_index(drop=True)

        train_trades = backtest_pair(train)
        test_trades = backtest_pair(test)

        train_m = compute_pair_metrics(train_trades)
        test_m = compute_pair_metrics(test_trades)

        if train_m["n"] >= 5 and test_m["n"] >= 3:
            # Correlation
            ratio = merged["a"] / merged["b"]
            corr = merged["a"].pct_change().corr(merged["b"].pct_change())

            split_results.append({
                "pair": f"{sym_a}/{sym_b}",
                "corr": corr,
                "train_n": train_m["n"],
                "train_wr": train_m["wr"],
                "train_sharpe": train_m["sharpe"],
                "train_pf": train_m["pf"],
                "train_avg_pnl": train_m["avg_pnl"],
                "test_n": test_m["n"],
                "test_wr": test_m["wr"],
                "test_sharpe": test_m["sharpe"],
                "test_pf": test_m["pf"],
                "test_avg_pnl": test_m["avg_pnl"],
                "test_reasons": test_m.get("reasons", {}),
            })

    if not split_results:
        print("No pairs with enough trades")
        return

    df = pd.DataFrame(split_results)
    df = df.sort_values("train_sharpe", ascending=False)

    # Summary
    print(f"\n{'='*90}")
    print(f"  RESULTS: {len(df)} pairs with enough trades")
    print(f"{'='*90}")

    print(f"\n  Top 15 by TRAIN Sharpe:")
    print(f"  {'Pair':25s} {'Corr':>5s} {'TR_N':>5s} {'TR_WR':>6s} {'TR_Sh':>6s} {'TR_PF':>6s} "
          f"{'TE_N':>5s} {'TE_WR':>6s} {'TE_Sh':>6s} {'TE_PF':>6s} {'TE_pnl':>7s}")
    print(f"  {'-'*88}")

    for _, r in df.head(15).iterrows():
        print(f"  {r['pair']:25s} {r['corr']:5.2f} "
              f"{r['train_n']:5.0f} {r['train_wr']:5.1%} {r['train_sharpe']:6.2f} {r['train_pf']:6.2f} "
              f"{r['test_n']:5.0f} {r['test_wr']:5.1%} {r['test_sharpe']:6.2f} {r['test_pf']:6.2f} "
              f"{r['test_avg_pnl']:+6.3f}%")

    # OOS survivors
    print(f"\n  OOS Survivors (train Sharpe > 0.5 AND test Sharpe > 0):")
    survivors = df[(df["train_sharpe"] > 0.5) & (df["test_sharpe"] > 0)]
    if len(survivors) == 0:
        print("  None")
    else:
        for _, r in survivors.iterrows():
            print(f"  {r['pair']:25s} corr={r['corr']:.2f} "
                  f"TRAIN: {r['train_n']:.0f}t Sh={r['train_sharpe']:.2f} "
                  f"TEST: {r['test_n']:.0f}t Sh={r['test_sharpe']:.2f} WR={r['test_wr']:.1%} "
                  f"PF={r['test_pf']:.2f} pnl={r['test_avg_pnl']:+.3f}%")

    # High correlation pairs analysis
    print(f"\n  High-corr pairs (>0.85) performance:")
    hc = df[df["corr"] > 0.85].sort_values("test_sharpe", ascending=False)
    for _, r in hc.head(10).iterrows():
        print(f"  {r['pair']:25s} corr={r['corr']:.2f} "
              f"test: {r['test_n']:.0f}t Sh={r['test_sharpe']:.2f} WR={r['test_wr']:.1%}")

    # Low correlation pairs (diversifiers)
    print(f"\n  Low-corr pairs (<0.60) performance:")
    lc = df[df["corr"] < 0.60].sort_values("test_sharpe", ascending=False)
    for _, r in lc.head(10).iterrows():
        print(f"  {r['pair']:25s} corr={r['corr']:.2f} "
              f"test: {r['test_n']:.0f}t Sh={r['test_sharpe']:.2f} WR={r['test_wr']:.1%}")

    # Save
    df.to_csv("data/reports/pairs_trading_results.csv", index=False)
    print(f"\n  Saved {len(df)} pairs to data/reports/pairs_trading_results.csv")


if __name__ == "__main__":
    main()
