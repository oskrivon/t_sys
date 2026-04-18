"""Train GradientBoosting classifier for Miro strategy signals.

Runs walk-forward backtest, collects features, trains model, saves to joblib.

Usage:
    python scripts/train_model.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, SignalType

# -- Config --
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]
TF = "4h"
MONTHS = 8
DATA = ROOT / "data" / "processed" / "candles"
MODELS = ROOT / "data" / "models"

LEVEL_LOOKBACK = 200
LEVEL_MIN_TOUCHES = 2
LEVEL_TOLERANCE_PCT = 1.0
RETEST_WINDOW = 15
MIN_LEVEL_AGE = 20
TREND_SMA = 50
RR_RATIO = 3.0
FEE_BPS = 10
CHECK_INTERVAL = 6


def fetch_ohlcv(symbol: str, tf: str, months: int) -> pd.DataFrame:
    """Download OHLCV data from Binance."""
    import ccxt
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


def load_data() -> dict[str, pd.DataFrame]:
    """Load or download OHLCV data for all symbols."""
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{TF}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
        else:
            sys.stdout.write(f"  Downloading {symbol} {TF}...")
            sys.stdout.flush()
            df = fetch_ohlcv(symbol, TF, MONTHS)
            if not df.empty:
                df.to_parquet(path, index=False)
                sys.stdout.write(f" {len(df)} candles\n")
            else:
                sys.stdout.write(" FAILED\n")
        if not df.empty:
            datasets[symbol] = df
    return datasets


def collect_trades_with_features(datasets: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Run walk-forward and collect features for each trade."""
    all_records = []

    for symbol, df in datasets.items():
        print(f"  {symbol}: {len(df)} candles")

        sma = df["close"].rolling(TREND_SMA).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts: list[Breakout] = []
        cached_levels = []
        last_check = 0
        warmup = max(LEVEL_LOOKBACK, TREND_SMA) + 10

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
                    elif i - at["entry_idx"] > 150:
                        outcome, exit_price = "timeout", df["close"].iloc[i]
                else:
                    if h >= at["sl"]:
                        outcome, exit_price = "loss", at["sl"]
                    elif l <= at["tp"]:
                        outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > 150:
                        outcome, exit_price = "timeout", df["close"].iloc[i]

                if outcome:
                    ep = at["entry_price"]
                    if at["is_long"]:
                        pnl_pct = (exit_price - ep) / ep - 2 * FEE_BPS / 10000
                    else:
                        pnl_pct = (ep - exit_price) / ep - 2 * FEE_BPS / 10000

                    record = {**at["features"], "outcome": outcome, "pnl_pct": pnl_pct,
                              "symbol": symbol, "signal_type": at["signal_type"]}
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            # -- Update levels --
            if i - last_check >= CHECK_INTERVAL:
                cached_levels = get_rolling_levels(
                    df, i, LEVEL_LOOKBACK, LEVEL_MIN_TOUCHES,
                    LEVEL_TOLERANCE_PCT, MIN_LEVEL_AGE,
                )
                last_check = i

                new_brk = detect_breakouts(df, i, cached_levels)
                recent_breakouts.extend(
                    Breakout(level=b.level, direction=b.direction, idx=b.idx)
                    for b in new_brk
                )
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= RETEST_WINDOW]

            # -- Detect signals --
            close_i = df["close"].iloc[i]

            retest = detect_retests(df, i, recent_breakouts, RETEST_WINDOW)
            zakol = detect_zakol(df, i, cached_levels)

            best = None
            if retest and zakol:
                best = retest if retest[2] >= zakol[2] else zakol
            else:
                best = retest or zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, RR_RATIO)
                if signal is None:
                    continue

                features = compute_features(df, i, lv, sig_type, TREND_SMA)

                active_trade = {
                    "entry_idx": i,
                    "entry_price": close_i,
                    "sl": signal.sl,
                    "tp": signal.tp,
                    "is_long": signal.is_long,
                    "features": features,
                    "signal_type": sig_type.value,
                }

    return pd.DataFrame(all_records)


def train_and_save(trades_df: pd.DataFrame) -> None:
    """Train GradientBoosting and save to joblib."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_predict
    import joblib

    trades_df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    trades_df["target"] = (trades_df["outcome"] == "win").astype(int)

    feature_cols = [c for c in trades_df.columns
                    if c not in ["outcome", "target", "pnl_pct", "symbol", "signal_type"]]

    X = trades_df[feature_cols].fillna(0).values
    y = trades_df["target"].values

    print(f"\n  Dataset: {len(trades_df)} trades ({y.sum()} wins, {len(y) - y.sum()} losses)")
    print(f"  Baseline WR: {y.mean() * 100:.1f}%")
    print(f"  Features: {len(feature_cols)}")

    clf = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=3,
        learning_rate=0.1,
        min_samples_leaf=10,
        subsample=0.8,
        random_state=42,
    )

    # Cross-validation evaluation
    y_prob = cross_val_predict(clf, X, y, cv=5, method="predict_proba")[:, 1]

    print(f"\n  === Threshold Analysis ===")
    print(f"  {'Threshold':>10} {'Trades':>7} {'WR':>6}")
    for threshold in [0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        mask = y_prob >= threshold
        if mask.sum() < 10:
            continue
        wr = y[mask].mean()
        print(f"  {threshold:>10.2f} {mask.sum():>7} {wr * 100:>5.1f}%")

    # Train final model on all data
    clf.fit(X, y)

    # Feature importance
    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 10 features:")
    for name, imp in fi[:10]:
        print(f"    {name:>25s}: {imp:.4f}")

    # Save
    MODELS.mkdir(parents=True, exist_ok=True)
    model_path = MODELS / "miro_gb_v1.joblib"
    joblib.dump(clf, model_path)
    print(f"\n  Model saved: {model_path}")

    # Also save feature names for reference
    import json
    meta_path = MODELS / "miro_gb_v1_meta.json"
    meta_path.write_text(json.dumps({
        "features": feature_cols,
        "n_trades": len(trades_df),
        "baseline_wr": float(y.mean()),
    }, indent=2))
    print(f"  Metadata saved: {meta_path}")


def main():
    print("=== Train Miro Strategy ML Model ===\n")
    print("Loading data...")
    datasets = load_data()

    print("\nCollecting trades with features...")
    trades_df = collect_trades_with_features(datasets)
    print(f"\nTotal trades: {len(trades_df)}")

    if len(trades_df) < 50:
        print("Not enough trades for ML training!")
        return

    print("\nTraining model...")
    train_and_save(trades_df)
    print("\nDone!")


if __name__ == "__main__":
    main()
