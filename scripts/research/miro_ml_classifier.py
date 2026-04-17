"""
ML classifier для Miro strategy signals.

Берёт trades из v3 (rolling levels walk-forward), считает features,
обучает XGBoost на win/loss, оценивает improvement в win rate.

Пример:
    python scripts/research/miro_ml_classifier.py
"""
from __future__ import annotations

import time
import sys
import math
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np

DATA = Path("data/processed/candles")
REPORTS = Path("data/reports")

# Import v3 components
sys.path.insert(0, str(Path(__file__).parent))
from miro_strategy_v3 import (
    load_data, get_rolling_levels, LEVEL_LOOKBACK, TREND_SMA,
    RETEST_WINDOW, TF, SYMBOLS, INITIAL_CAPITAL, RR_RATIO,
    MAX_RISK_PCT, FEE_BPS, is_round_number,
    find_swing_points_range, cluster_points, LEVEL_TOLERANCE_PCT,
    LEVEL_MIN_TOUCHES, MIN_LEVEL_AGE,
)


def compute_features(df: pd.DataFrame, signal: dict, idx: int) -> dict:
    """Compute features for a single trade signal."""
    f = {}

    close = df["close"].iloc[idx]
    high = df["high"].iloc[idx]
    low = df["low"].iloc[idx]
    open_ = df["open"].iloc[idx]
    volume = df["volume"].iloc[idx]

    level = signal.get("level", {})

    # ── Level features ──
    f["level_touches"] = level.get("touches", 0)
    f["level_score"] = level.get("score", 0)
    f["level_age"] = idx - level.get("last_idx", idx)
    f["is_round_number"] = 1 if is_round_number(level.get("price", 0)) else 0

    zone_w = level.get("zone_high", close) - level.get("zone_low", close)
    if zone_w <= 0:
        zone_w = close * 0.005
    f["zone_width_pct"] = zone_w / close * 100

    # Distance from entry to level
    f["distance_to_level_pct"] = abs(close - level.get("price", close)) / close * 100

    # ── Signal type ──
    sig_type = signal.get("type", "unknown")
    f["is_retest"] = 1 if "retest" in sig_type else 0
    f["is_zakol"] = 1 if "zakol" in sig_type else 0
    f["is_long"] = 1 if "long" in sig_type else 0

    # ── Candle features ──
    candle_range = high - low
    if candle_range > 0:
        f["body_ratio"] = abs(close - open_) / candle_range
        f["upper_wick_ratio"] = (high - max(close, open_)) / candle_range
        f["lower_wick_ratio"] = (min(close, open_) - low) / candle_range
    else:
        f["body_ratio"] = 0
        f["upper_wick_ratio"] = 0
        f["lower_wick_ratio"] = 0

    f["is_green"] = 1 if close > open_ else 0

    # ── Trend features ──
    sma_50 = df["close"].iloc[max(0, idx - TREND_SMA):idx].mean() if idx >= TREND_SMA else close
    f["distance_to_sma50_pct"] = (close - sma_50) / sma_50 * 100

    sma_20 = df["close"].iloc[max(0, idx - 20):idx].mean() if idx >= 20 else close
    f["distance_to_sma20_pct"] = (close - sma_20) / sma_20 * 100

    # Trend direction: count green vs red in last 10 candles
    if idx >= 10:
        recent = df.iloc[idx - 10:idx]
        f["green_candles_10"] = (recent["close"] > recent["open"]).sum()
        f["trend_strength_10"] = (df["close"].iloc[idx] - df["close"].iloc[idx - 10]) / df["close"].iloc[idx - 10] * 100
    else:
        f["green_candles_10"] = 5
        f["trend_strength_10"] = 0

    # ── Volatility features ──
    if idx >= 20:
        returns = df["close"].iloc[idx - 20:idx].pct_change().dropna()
        f["volatility_20"] = returns.std() * 100
        f["atr_20"] = (df["high"].iloc[idx - 20:idx] - df["low"].iloc[idx - 20:idx]).mean() / close * 100
    else:
        f["volatility_20"] = 1.0
        f["atr_20"] = 1.0

    f["atr_vs_zone"] = f["atr_20"] / f["zone_width_pct"] if f["zone_width_pct"] > 0 else 1.0

    # ── Volume features ──
    if idx >= 30:
        avg_vol_30 = df["volume"].iloc[idx - 30:idx].mean()
        f["volume_ratio"] = volume / avg_vol_30 if avg_vol_30 > 0 else 1.0
        # Volume trend
        vol_recent = df["volume"].iloc[idx - 5:idx].mean()
        vol_older = df["volume"].iloc[idx - 30:idx - 5].mean()
        f["volume_trend"] = vol_recent / vol_older if vol_older > 0 else 1.0
    else:
        f["volume_ratio"] = 1.0
        f["volume_trend"] = 1.0

    # ── "Coin in play" proxy features ──
    if idx >= 30:
        # Price change over different periods
        f["price_change_7d"] = (close / df["close"].iloc[max(0, idx - 42)] - 1) * 100  # 42 x 4h = 7d
        f["price_change_30d"] = (close / df["close"].iloc[max(0, idx - 180)] - 1) * 100  # 180 x 4h = 30d
        # Absolute move (we want coins that MOVED, direction doesn't matter for "in play")
        f["abs_move_7d"] = abs(f["price_change_7d"])
        f["abs_move_30d"] = abs(f["price_change_30d"])
    else:
        f["price_change_7d"] = 0
        f["price_change_30d"] = 0
        f["abs_move_7d"] = 0
        f["abs_move_30d"] = 0

    # ── RSI ──
    if idx >= 14:
        deltas = df["close"].iloc[idx - 14:idx + 1].diff().dropna()
        gains = deltas.clip(lower=0).mean()
        losses = (-deltas.clip(upper=0)).mean()
        rs = gains / losses if losses > 0 else 100
        f["rsi_14"] = 100 - (100 / (1 + rs))
    else:
        f["rsi_14"] = 50

    # ── Time features ──
    ts = df["ts"].iloc[idx]
    if hasattr(ts, "hour"):
        f["hour_utc"] = ts.hour
        f["day_of_week"] = ts.dayofweek
    else:
        f["hour_utc"] = 12
        f["day_of_week"] = 3

    return f


def collect_trades_with_features(datasets: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Run v3 walk-forward and collect features for each trade."""
    from miro_strategy_v3 import (
        get_rolling_levels, MIN_LEVEL_AGE, RETEST_WINDOW,
    )

    all_records = []

    for symbol, df in datasets.items():
        print(f"  {symbol}: {len(df)} candles")

        # Precompute trend
        sma = df["close"].rolling(TREND_SMA).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        capital = INITIAL_CAPITAL
        active_trade = None
        recent_breakouts = []
        cached_levels = []
        last_check = 0
        CHECK_INTERVAL = 6

        warmup = max(LEVEL_LOOKBACK, TREND_SMA) + 10

        for i in range(warmup, len(df)):
            # ── Manage active trade ──
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
                    if at["is_long"]:
                        pnl_pct = (exit_price - at["entry_price"]) / at["entry_price"] - 2 * FEE_BPS / 10000
                    else:
                        pnl_pct = (at["entry_price"] - exit_price) / at["entry_price"] - 2 * FEE_BPS / 10000

                    record = {**at["features"], "outcome": outcome, "pnl_pct": pnl_pct,
                              "symbol": symbol, "signal_type": at["signal_type"]}
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            # ── Update levels ──
            if i - last_check >= CHECK_INTERVAL:
                cached_levels = get_rolling_levels(df, i)
                last_check = i

                close_i = df["close"].iloc[i]
                prev_close = df["close"].iloc[i - 1]
                for lv in cached_levels:
                    zh, zl = lv["zone_high"], lv["zone_low"]
                    if prev_close <= zh and close_i > zh * 1.001:
                        recent_breakouts.append({"level": lv, "dir": "long", "idx": i})
                    if prev_close >= zl and close_i < zl * 0.999:
                        recent_breakouts.append({"level": lv, "dir": "short", "idx": i})
                recent_breakouts = [b for b in recent_breakouts if i - b["idx"] <= RETEST_WINDOW]

            # ── Detect signals ──
            close_i = df["close"].iloc[i]
            low_i, high_i = df["low"].iloc[i], df["high"].iloc[i]
            t = trend.iloc[i]

            best_signal = None
            best_score = 0

            for brk in recent_breakouts:
                lv = brk["level"]
                zh, zl = lv["zone_high"], lv["zone_low"]
                age = i - brk["idx"]
                if age < 1 or age > RETEST_WINDOW:
                    continue

                if brk["dir"] == "long" and low_i <= zh * 1.005 and close_i > zh:
                    if lv["score"] > best_score:
                        best_signal = {"type": "long_retest", "level": lv}
                        best_score = lv["score"]

                if brk["dir"] == "short" and high_i >= zl * 0.995 and close_i < zl:
                    if lv["score"] > best_score:
                        best_signal = {"type": "short_retest", "level": lv}
                        best_score = lv["score"]

            for lv in cached_levels:
                zh, zl = lv["zone_high"], lv["zone_low"]
                if low_i < zl * 0.995 and close_i > zl:
                    if lv["score"] + 1 > best_score:
                        best_signal = {"type": "long_zakol", "level": lv}
                        best_score = lv["score"] + 1
                if high_i > zh * 1.005 and close_i < zh:
                    if lv["score"] + 1 > best_score:
                        best_signal = {"type": "short_zakol", "level": lv}
                        best_score = lv["score"] + 1

            if best_signal and capital > 100:
                lv = best_signal["level"]
                is_long = "long" in best_signal["type"]
                zh, zl = lv["zone_high"], lv["zone_low"]
                zone_w = zh - zl
                if zone_w < close_i * 0.003:
                    zone_w = close_i * 0.005

                if is_long:
                    sl = zl - zone_w * 0.3
                    sl_dist = close_i - sl
                    tp = close_i + sl_dist * RR_RATIO
                else:
                    sl = zh + zone_w * 0.3
                    sl_dist = sl - close_i
                    tp = close_i - sl_dist * RR_RATIO

                if sl_dist <= 0 or sl_dist / close_i > 0.08:
                    continue

                # Compute features
                features = compute_features(df, best_signal, i)

                risk_amt = capital * MAX_RISK_PCT / 100
                pos_size = risk_amt / (sl_dist / close_i)
                pos_size = min(pos_size, capital * 0.90)

                active_trade = {
                    "entry_idx": i,
                    "entry_price": close_i,
                    "sl": sl,
                    "tp": tp,
                    "is_long": is_long,
                    "position_size": pos_size,
                    "features": features,
                    "signal_type": best_signal["type"],
                }

    return pd.DataFrame(all_records)


def run_ml(trades_df: pd.DataFrame) -> None:
    """Train XGBoost classifier and evaluate."""
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_predict
        from sklearn.metrics import classification_report
    except ImportError:
        print("Installing scikit-learn...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "scikit-learn", "-q"])
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_predict
        from sklearn.metrics import classification_report

    # Prepare data
    trades_df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    trades_df["target"] = (trades_df["outcome"] == "win").astype(int)

    feature_cols = [c for c in trades_df.columns
                    if c not in ["outcome", "target", "pnl_pct", "symbol", "signal_type"]]

    X = trades_df[feature_cols].fillna(0).values
    y = trades_df["target"].values

    print(f"\n  Dataset: {len(trades_df)} trades ({y.sum()} wins, {len(y) - y.sum()} losses)")
    print(f"  Baseline WR: {y.mean()*100:.1f}%")
    print(f"  Features: {len(feature_cols)}")

    # ── Cross-validated predictions ──
    clf = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=3,
        learning_rate=0.1,
        min_samples_leaf=10,
        subsample=0.8,
        random_state=42,
    )

    # 5-fold cross-validation predictions
    y_prob = cross_val_predict(clf, X, y, cv=5, method="predict_proba")[:, 1]

    # ── Threshold analysis ──
    print(f"\n  === Threshold Analysis ===")
    print(f"  {'Threshold':>10} {'Trades':>7} {'WR':>6} {'Exp/trade':>10} {'Monthly':>8} {'Annual':>8}")

    best_annual = -999
    best_threshold = 0.3

    for threshold in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        mask = y_prob >= threshold
        if mask.sum() < 10:
            continue
        filtered_y = y[mask]
        filtered_pnl = trades_df["pnl_pct"].values[mask]
        wr = filtered_y.mean()
        n = mask.sum()

        avg_win = filtered_pnl[filtered_y == 1].mean() if (filtered_y == 1).sum() > 0 else 0
        avg_loss = abs(filtered_pnl[filtered_y == 0].mean()) if (filtered_y == 0).sum() > 0 else 0
        exp = wr * avg_win - (1 - wr) * avg_loss

        # Estimate trades per month (proportional reduction)
        period_months = 6.8  # approx period
        tpm = n / period_months
        monthly = tpm * exp * MAX_RISK_PCT / 100
        annual = (1 + monthly) ** 12 - 1

        marker = " <-- best" if annual > best_annual else ""
        if annual > best_annual:
            best_annual = annual
            best_threshold = threshold

        print(f"  {threshold:>10.2f} {n:>7} {wr*100:>5.1f}% {exp*100:>+9.3f}% {monthly*100:>+7.2f}% {annual*100:>+7.1f}%{marker}")

    # ── Feature importance ──
    clf.fit(X, y)
    importances = clf.feature_importances_
    fi = sorted(zip(feature_cols, importances), key=lambda x: x[1], reverse=True)

    print(f"\n  === Feature Importance (top 15) ===")
    for name, imp in fi[:15]:
        bar = "#" * int(imp * 200)
        print(f"  {name:>25s}: {imp:.4f} {bar}")

    # ── Best threshold details ──
    print(f"\n  === Best Threshold: {best_threshold:.2f} ===")
    mask = y_prob >= best_threshold
    if mask.sum() > 0:
        fy = y[mask]
        fp = trades_df["pnl_pct"].values[mask]
        fs = trades_df["symbol"].values[mask]

        wr = fy.mean()
        aw = fp[fy == 1].mean() if (fy == 1).sum() > 0 else 0
        al = abs(fp[fy == 0].mean()) if (fy == 0).sum() > 0 else 0
        exp = wr * aw - (1 - wr) * al

        print(f"  Trades: {mask.sum()} (filtered from {len(y)})")
        print(f"  Win rate: {wr*100:.1f}% (was {y.mean()*100:.1f}%)")
        print(f"  Avg win: {aw*100:+.2f}%, Avg loss: -{al*100:.2f}%")
        print(f"  Expectancy: {exp*100:+.3f}%/trade")

        # Per-symbol breakdown
        print(f"\n  Per-symbol after filter:")
        for sym in sorted(set(fs)):
            sm = (fs == sym)
            sw = fy[sm].mean() * 100
            sn = sm.sum()
            print(f"    {sym:12s}: {sn:3d} trades, {sw:.0f}% WR")

    # ── Return estimation with ML filter ──
    print(f"\n  === RETURN ESTIMATION WITH ML ===")
    period_months = 6.8
    for scenario, wr_adj in [("pessimistic", -0.05), ("base", 0), ("optimistic", +0.05)]:
        m = y_prob >= best_threshold
        if m.sum() == 0:
            continue
        base_wr = y[m].mean() + wr_adj
        aw = trades_df["pnl_pct"].values[m][y[m] == 1].mean() if (y[m] == 1).sum() > 0 else 0
        al = abs(trades_df["pnl_pct"].values[m][y[m] == 0].mean()) if (y[m] == 0).sum() > 0 else 0
        adj_exp = base_wr * aw - (1 - base_wr) * al
        tpm = m.sum() / period_months
        monthly = tpm * adj_exp * MAX_RISK_PCT / 100
        annual = (1 + monthly) ** 12 - 1
        print(f"    {scenario:12s}: WR={base_wr*100:.0f}%, {tpm:.0f} trades/mo, "
              f"monthly={monthly*100:+.2f}%, annual={annual*100:+.1f}%")

    # Write report
    _write_report(trades_df, y, y_prob, feature_cols, fi, best_threshold)


def _write_report(tdf, y, y_prob, features, fi, best_thr):
    lines = [
        "# Miro Strategy ML Classifier Results",
        "",
        f"**Model:** GradientBoosting (100 trees, depth=3)",
        f"**Evaluation:** 5-fold cross-validation",
        f"**Features:** {len(features)}",
        f"**Dataset:** {len(tdf)} trades ({y.sum()} wins, {len(y)-y.sum()} losses)",
        f"**Baseline WR:** {y.mean()*100:.1f}%",
        "",
        "## Feature Importance (top 10)",
        "",
        "| Rank | Feature | Importance |",
        "|---:|---|---:|",
    ]
    for i, (name, imp) in enumerate(fi[:10]):
        lines.append(f"| {i+1} | {name} | {imp:.4f} |")

    mask = y_prob >= best_thr
    if mask.sum() > 0:
        wr = y[mask].mean()
        lines += [
            "",
            f"## Best Threshold: {best_thr:.2f}",
            f"- Trades after filter: {mask.sum()} / {len(y)}",
            f"- Win rate: **{wr*100:.1f}%** (was {y.mean()*100:.1f}%)",
            f"- Improvement: **+{(wr - y.mean())*100:.1f}pp**",
            "",
        ]

    out = REPORTS / "miro_ml_classifier.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


def run() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Miro ML Classifier ===\n")
    print("Loading data...")
    datasets = load_data()

    print("\nCollecting trades with features...")
    trades_df = collect_trades_with_features(datasets)

    print(f"\nTotal trades collected: {len(trades_df)}")
    if len(trades_df) < 50:
        print("Not enough trades for ML!")
        return

    print("\nTraining ML classifier...")
    run_ml(trades_df)


if __name__ == "__main__":
    run()
