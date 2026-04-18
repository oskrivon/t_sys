"""Train ML model for D1 level mode (4H entries at D1 levels).

Usage:
    python scripts/train_model_d1.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

DATA = ROOT / "data" / "processed" / "candles"
MODELS = ROOT / "data" / "models"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10
# 4H entry params
TREND_SMA = 50
RR_RATIO = 3.0
RETEST_WINDOW = 15
CHECK_INTERVAL = 6
MAX_HOLD = 150
# D1 level params
D1_LOOKBACK = 120
D1_TOLERANCE = 1.5
D1_MIN_AGE = 5


def load_data(tf):
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{tf}.parquet"
        if path.exists():
            datasets[symbol] = pd.read_parquet(path)
    return datasets


def get_d1_levels_at_time(d1_df, current_ts):
    mask = d1_df["ts"] <= current_ts
    if mask.sum() < 50:
        return []
    d1_sub = d1_df[mask]
    return get_rolling_levels(d1_sub, len(d1_sub) - 1,
                              lookback=D1_LOOKBACK, min_touches=2,
                              tolerance_pct=D1_TOLERANCE, min_level_age=D1_MIN_AGE,
                              swing_order=3)


def collect_trades(data_4h, d1_data):
    all_records = []
    for symbol in SYMBOLS:
        if symbol not in data_4h or symbol not in d1_data:
            continue
        df = data_4h[symbol]
        d1_df = d1_data[symbol]
        print(f"  {symbol}: {len(df)} candles")

        sma = df["close"].rolling(TREND_SMA).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts: list[Breakout] = []
        cached_levels = []
        last_check = 0
        last_d1_ts = None
        cached_d1 = []
        warmup = max(200, TREND_SMA) + 10

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

            # D1 levels (update daily)
            ts = df["ts"].iloc[i]
            if last_d1_ts is None or (ts - last_d1_ts).total_seconds() > 86400:
                cached_d1 = get_d1_levels_at_time(d1_df, ts)
                last_d1_ts = ts

            # Use D1 levels directly (d1_only mode)
            if i - last_check >= CHECK_INTERVAL:
                cached_levels = cached_d1
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
                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "features": features, "signal_type": sig_type.value,
                }

    return pd.DataFrame(all_records)


def train_and_save(trades_df):
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
        min_samples_leaf=max(5, len(trades_df)//50), subsample=0.8, random_state=42,
    )

    cv = min(5, max(2, len(trades_df) // 30))
    y_prob = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")[:, 1]

    print(f"\n  {'Thr':>6} {'N':>6} {'WR':>6} {'PF':>6}")
    for thr in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        mask = y_prob >= thr
        if mask.sum() < 8:
            continue
        fy = y[mask]
        fp = trades_df["pnl_pct"].values[mask]
        gw = fp[fy == 1].sum() if (fy == 1).sum() > 0 else 0
        gl = abs(fp[fy == 0].sum()) if (fy == 0).sum() > 0 else 1
        pf = gw / gl
        print(f"  {thr:>6.2f} {mask.sum():>6} {fy.mean()*100:>5.1f}% {pf:>5.2f}")

    clf.fit(X, y)

    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 10 features:")
    for name, imp in fi[:10]:
        print(f"    {name:>25s}: {imp:.4f}")

    MODELS.mkdir(parents=True, exist_ok=True)
    model_path = MODELS / "miro_gb_d1.joblib"
    joblib.dump(clf, model_path)
    print(f"\n  Model saved: {model_path}")

    import json
    meta_path = MODELS / "miro_gb_d1_meta.json"
    meta_path.write_text(json.dumps({
        "features": feature_cols, "n_trades": len(trades_df),
        "baseline_wr": float(y.mean()), "mode": "d1_only", "tf": "4h",
    }, indent=2))
    print(f"  Metadata saved: {meta_path}")


def main():
    print("=== Train D1 Level ML Model ===\n")
    data_4h = load_data("4h")
    d1_data = load_data("1d")
    print(f"4H: {len(data_4h)} symbols, D1: {len(d1_data)} symbols\n")

    print("Collecting trades (D1 levels, 4H entries)...")
    trades_df = collect_trades(data_4h, d1_data)
    print(f"\nTotal: {len(trades_df)} trades")

    if len(trades_df) < 30:
        print("Not enough trades!")
        return

    print("\nTraining...")
    train_and_save(trades_df)
    print("\nDone!")


if __name__ == "__main__":
    main()
