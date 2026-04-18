"""Train GradientBoosting classifier for 1H scalp signals.

Usage:
    python scripts/train_model_1h.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import ccxt

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout

DATA = ROOT / "data" / "processed" / "candles"
MODELS = ROOT / "data" / "models"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]
TF = "1h"
MONTHS = 4
FEE_BPS = 10

# 1H scalp params
LEVEL_LOOKBACK = 72
LEVEL_MIN_TOUCHES = 2
LEVEL_TOLERANCE_PCT = 0.5
RETEST_WINDOW = 6
MIN_LEVEL_AGE = 6
TREND_SMA = 20
RR_RATIO = 2.0
MAX_RISK_PCT = 2.0
CHECK_INTERVAL = 2
MAX_HOLD = 24


def fetch_ohlcv(symbol: str) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - MONTHS * 30 * 86400 * 1000
    all_c = []
    cur = since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, TF, since=cur, limit=1000)
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
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{TF}.parquet"
        if path.exists():
            datasets[symbol] = pd.read_parquet(path)
            continue
        sys.stdout.write(f"  Downloading {symbol} {TF}...")
        sys.stdout.flush()
        df = fetch_ohlcv(symbol)
        if not df.empty:
            df.to_parquet(path, index=False)
            sys.stdout.write(f" {len(df)} candles\n")
            datasets[symbol] = df
        else:
            sys.stdout.write(" FAILED\n")
    return datasets


def collect_trades(datasets: dict[str, pd.DataFrame]) -> pd.DataFrame:
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
            if active_trade is not None:
                h, l = df["high"].iloc[i], df["low"].iloc[i]
                at = active_trade
                outcome, exit_price = None, None
                if at["is_long"]:
                    if l <= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif h >= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > MAX_HOLD: outcome, exit_price = "timeout", df["close"].iloc[i]
                else:
                    if h >= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif l <= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > MAX_HOLD: outcome, exit_price = "timeout", df["close"].iloc[i]

                if outcome:
                    ep = at["entry_price"]
                    pnl_pct = ((exit_price - ep) / ep if at["is_long"] else (ep - exit_price) / ep) - 2 * FEE_BPS / 10000
                    record = {**at["features"], "outcome": outcome, "pnl_pct": pnl_pct,
                              "symbol": symbol, "signal_type": at["signal_type"]}
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            if i - last_check >= CHECK_INTERVAL:
                cached_levels = get_rolling_levels(
                    df, i, LEVEL_LOOKBACK, LEVEL_MIN_TOUCHES,
                    LEVEL_TOLERANCE_PCT, MIN_LEVEL_AGE,
                )
                last_check = i
                new_brk = detect_breakouts(df, i, cached_levels)
                recent_breakouts.extend(new_brk)
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= RETEST_WINDOW]

            close_i = df["close"].iloc[i]
            retest = detect_retests(df, i, recent_breakouts, RETEST_WINDOW, trend)
            zakol = detect_zakol(df, i, cached_levels, trend)
            best = retest if retest and (not zakol or retest[2] >= zakol[2]) else zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, RR_RATIO)
                if signal is None:
                    continue
                features = compute_features(df, i, lv, sig_type, TREND_SMA)
                sl_dist = abs(close_i - signal.sl)
                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "features": features, "signal_type": sig_type.value,
                }

    return pd.DataFrame(all_records)


def train_and_save(trades_df: pd.DataFrame) -> None:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_predict
    import joblib

    trades_df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    trades_df["target"] = (trades_df["outcome"] == "win").astype(int)

    feature_cols = [c for c in trades_df.columns
                    if c not in ["outcome", "target", "pnl_pct", "symbol", "signal_type"]]

    X = trades_df[feature_cols].fillna(0).values
    y = trades_df["target"].values

    print(f"\n  Dataset: {len(trades_df)} trades ({y.sum()} wins, {len(y)-y.sum()} losses)")
    print(f"  Baseline WR: {y.mean()*100:.1f}%, Features: {len(feature_cols)}")

    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        min_samples_leaf=10, subsample=0.8, random_state=42,
    )

    y_prob = cross_val_predict(clf, X, y, cv=5, method="predict_proba")[:, 1]

    print(f"\n  {'Thr':>6} {'N':>6} {'WR':>6}")
    for thr in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]:
        mask = y_prob >= thr
        if mask.sum() >= 10:
            print(f"  {thr:>6.2f} {mask.sum():>6} {y[mask].mean()*100:>5.1f}%")

    clf.fit(X, y)

    MODELS.mkdir(parents=True, exist_ok=True)
    model_path = MODELS / "miro_gb_1h_scalp.joblib"
    joblib.dump(clf, model_path)
    print(f"\n  Model saved: {model_path}")

    import json
    meta_path = MODELS / "miro_gb_1h_scalp_meta.json"
    meta_path.write_text(json.dumps({
        "features": feature_cols,
        "n_trades": len(trades_df),
        "baseline_wr": float(y.mean()),
        "tf": TF,
        "params": {
            "level_lookback": LEVEL_LOOKBACK,
            "level_tolerance_pct": LEVEL_TOLERANCE_PCT,
            "retest_window": RETEST_WINDOW,
            "min_level_age": MIN_LEVEL_AGE,
            "trend_sma": TREND_SMA,
            "rr_ratio": RR_RATIO,
            "max_hold": MAX_HOLD,
        },
    }, indent=2))
    print(f"  Metadata saved: {meta_path}")


def main():
    print("=== Train 1H Scalp ML Model ===\n")
    datasets = load_data()
    print("\nCollecting trades...")
    trades_df = collect_trades(datasets)
    print(f"\nTotal: {len(trades_df)} trades")
    if len(trades_df) < 50:
        print("Not enough trades!")
        return
    print("\nTraining...")
    train_and_save(trades_df)
    print("\nDone!")


if __name__ == "__main__":
    main()
