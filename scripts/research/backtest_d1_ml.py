"""
Backtest D1 filter + ML combined.

Tests the full pipeline: D1 level confirmation + ML probability filter.

Usage:
    python scripts/research/backtest_d1_ml.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import ccxt

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

DATA = ROOT / "data" / "processed" / "candles"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10
INITIAL_CAPITAL = 10_000


def load_data(tf: str) -> dict[str, pd.DataFrame]:
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
    d1_subset = d1_df[mask]
    return get_rolling_levels(d1_subset, len(d1_subset) - 1,
                              lookback=120, min_touches=2, tolerance_pct=1.5,
                              min_level_age=5, swing_order=3)


def level_near_d1(level, d1_levels, pct=2.0):
    for d1 in d1_levels:
        if abs(level.price - d1.price) / level.price * 100 < pct:
            return True
    return False


def collect_trades_with_features(entry_data, d1_data, tf, mode, params):
    """Walk-forward, collect trades with features + d1 alignment flag."""
    all_records = []

    for symbol in SYMBOLS:
        if symbol not in entry_data or symbol not in d1_data:
            continue
        df = entry_data[symbol]
        d1_df = d1_data[symbol]

        sma = df["close"].rolling(params["trend_sma"]).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts: list[Breakout] = []
        cached_levels = []
        cached_d1_levels = []
        last_check = 0
        last_d1_ts = None
        warmup = max(params["level_lookback"], params["trend_sma"]) + 10

        for i in range(warmup, len(df)):
            if active_trade is not None:
                h, l = df["high"].iloc[i], df["low"].iloc[i]
                at = active_trade
                outcome, exit_price = None, None
                if at["is_long"]:
                    if l <= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif h >= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > params["max_hold"]: outcome, exit_price = "timeout", df["close"].iloc[i]
                else:
                    if h >= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif l <= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > params["max_hold"]: outcome, exit_price = "timeout", df["close"].iloc[i]

                if outcome:
                    ep = at["entry_price"]
                    pnl_pct = ((exit_price - ep) / ep if at["is_long"] else (ep - exit_price) / ep) - 2 * FEE_BPS / 10000
                    record = {
                        **at["features"],
                        "outcome": outcome, "pnl_pct": pnl_pct,
                        "symbol": symbol, "signal_type": at["signal_type"],
                        "d1_aligned": at["d1_aligned"],
                        "hold_candles": i - at["entry_idx"],
                    }
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            # D1 levels
            ts = df["ts"].iloc[i]
            if last_d1_ts is None or (ts - last_d1_ts).total_seconds() > 86400:
                cached_d1_levels = get_d1_levels_at_time(d1_df, ts)
                last_d1_ts = ts

            # Entry TF levels
            if i - last_check >= params["check_interval"]:
                if mode == "d1_only":
                    cached_levels = cached_d1_levels
                elif mode == "d1_filter":
                    raw = get_rolling_levels(df, i, params["level_lookback"], 2,
                                             params["level_tolerance_pct"], params["min_level_age"])
                    cached_levels = [lv for lv in raw if level_near_d1(lv, cached_d1_levels)]
                else:  # base
                    cached_levels = get_rolling_levels(df, i, params["level_lookback"], 2,
                                                       params["level_tolerance_pct"], params["min_level_age"])
                last_check = i

                new_brk = detect_breakouts(df, i, cached_levels)
                recent_breakouts.extend(new_brk)
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= params["retest_window"]]

            close_i = df["close"].iloc[i]
            retest = detect_retests(df, i, recent_breakouts, params["retest_window"], trend)
            zakol = detect_zakol(df, i, cached_levels, trend)
            best = retest if retest and (not zakol or retest[2] >= zakol[2]) else zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, params["rr_ratio"])
                if signal is None:
                    continue

                features = compute_features(df, i, lv, sig_type, params["trend_sma"])
                d1_aligned = level_near_d1(lv, cached_d1_levels)

                # Add d1 as feature
                features["d1_aligned"] = 1.0 if d1_aligned else 0.0

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "features": features,
                    "d1_aligned": d1_aligned,
                }

    return pd.DataFrame(all_records)


def run_ml_analysis(trades_df, name, risk_pct, hpc, period_months):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_predict

    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    df["target"] = (df["outcome"] == "win").astype(int)

    feature_cols = [c for c in df.columns
                    if c not in ["outcome", "target", "pnl_pct", "symbol", "signal_type",
                                 "d1_aligned", "hold_candles"]]

    X = df[feature_cols].fillna(0).values
    y = df["target"].values

    print(f"\n  {name}: {len(df)} trades ({y.sum()}W/{len(y)-y.sum()}L), baseline WR {y.mean()*100:.1f}%")

    if len(df) < 30:
        print("    Too few trades for ML")
        return []

    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        min_samples_leaf=max(5, len(df)//50), subsample=0.8, random_state=42,
    )

    cv = min(5, max(2, len(df) // 30))
    y_prob = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")[:, 1]

    results = []
    print(f"  {'Thr':>6} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Mo':>7} {'Ann':>7} {'Hold':>6}")
    for thr in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        mask = y_prob >= thr
        if mask.sum() < 8:
            continue
        fy = y[mask]
        fp = df["pnl_pct"].values[mask]
        fh = df["hold_candles"].values[mask]
        wr = fy.mean()
        aw = fp[fy == 1].mean() if (fy == 1).sum() > 0 else 0
        al = abs(fp[fy == 0].mean()) if (fy == 0).sum() > 0 else 0
        exp = wr * aw - (1 - wr) * al
        tpm = mask.sum() / period_months
        monthly = tpm * exp * risk_pct / 100
        annual = (1 + monthly) ** 12 - 1

        gw = fp[fy == 1].sum() if (fy == 1).sum() > 0 else 0
        gl = abs(fp[fy == 0].sum()) if (fy == 0).sum() > 0 else 1
        pf = gw / gl if gl > 0 else 0

        avg_h = fh.mean() * hpc

        print(f"  {thr:>6.2f} {mask.sum():>5} {tpm:>4.0f} {wr*100:>5.1f}% {pf:>5.2f} "
              f"{exp*100:>+8.3f}% {monthly*100:>+6.2f}% {annual*100:>+6.1f}% {avg_h:>5.0f}h")

        results.append({"thr": thr, "n": int(mask.sum()), "tpm": tpm, "wr": wr,
                         "pf": pf, "exp": exp, "monthly": monthly, "annual": annual})

    # Feature importance
    clf.fit(X, y)
    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top features: ", end="")
    print(", ".join(f"{n}={v:.3f}" for n, v in fi[:8]))

    # Per-symbol at best threshold
    best = max(results, key=lambda r: r["annual"]) if results else None
    if best:
        thr = best["thr"]
        mask = y_prob >= thr
        fs = df["symbol"].values[mask]
        fy = y[mask]
        print(f"\n  Per-symbol at thr={thr:.2f}:")
        for sym in sorted(set(fs)):
            sm = fs == sym
            if sm.sum() >= 3:
                print(f"    {sym:12s}: {sm.sum():3d}t, {fy[sm].mean()*100:.0f}% WR")

    return results


def main():
    print("=== D1 + ML Combined Backtest ===\n")

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    data_1h = load_data("1h")

    print(f"D1: {len(d1_data)} symbols, 4H: {len(data_4h)}, 1H: {len(data_1h)}")

    configs = [
        # (name, entry_data, tf, mode, params, risk_pct, hpc, period_months)
        ("4H/base", data_4h, "4h", "base",
         dict(level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
              min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150),
         3.0, 4, 6.8),

        ("4H/d1_filter", data_4h, "4h", "d1_filter",
         dict(level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
              min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150),
         3.0, 4, 6.8),

        ("4H/d1_only", data_4h, "4h", "d1_only",
         dict(level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
              min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150),
         3.0, 4, 6.8),

        ("1H_scalp/base", data_1h, "1h", "base",
         dict(level_lookback=72, level_tolerance_pct=0.5, retest_window=6,
              min_level_age=6, trend_sma=20, rr_ratio=2.0, check_interval=2, max_hold=24),
         2.0, 1, 3.4),

        ("1H_scalp/d1_filter", data_1h, "1h", "d1_filter",
         dict(level_lookback=72, level_tolerance_pct=0.5, retest_window=6,
              min_level_age=6, trend_sma=20, rr_ratio=2.0, check_interval=2, max_hold=24),
         2.0, 1, 3.4),

        ("1H_scalp/d1_only", data_1h, "1h", "d1_only",
         dict(level_lookback=72, level_tolerance_pct=0.5, retest_window=6,
              min_level_age=6, trend_sma=20, rr_ratio=2.0, check_interval=2, max_hold=24),
         2.0, 1, 3.4),
    ]

    all_summaries = []

    for name, entry_data, tf, mode, params, risk_pct, hpc, period_months in configs:
        print(f"\n{'='*70}")
        print(f"  {name}")
        print(f"{'='*70}")

        print("  Collecting trades...")
        trades_df = collect_trades_with_features(entry_data, d1_data, tf, mode, params)
        print(f"  Total: {len(trades_df)}")

        results = run_ml_analysis(trades_df, name, risk_pct, hpc, period_months)
        if results:
            best = max(results, key=lambda r: r["annual"])
            all_summaries.append({"name": name, **best})

    # Final comparison
    if all_summaries:
        print(f"\n{'='*70}")
        print(f"  BEST RESULT PER CONFIG")
        print(f"{'='*70}")
        print(f"  {'Config':<22} {'Thr':>5} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Annual':>8}")
        for s in sorted(all_summaries, key=lambda x: x["annual"], reverse=True):
            print(f"  {s['name']:<22} {s['thr']:>5.2f} {s['n']:>5} {s['tpm']:>4.0f} "
                  f"{s['wr']*100:>5.1f}% {s['pf']:>5.2f} {s['exp']*100:>+8.3f}% {s['annual']*100:>+7.1f}%")


if __name__ == "__main__":
    main()
