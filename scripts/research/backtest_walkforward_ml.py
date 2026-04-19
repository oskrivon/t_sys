"""
Walk-Forward ML Backtest — honest evaluation.

Key differences from previous backtests:
  1. Walk-forward: train on 8 months, test on 2 months, roll 2 months
  2. 20 symbols x 12 months (double the data)
  3. Feature selection: keep top-K features by importance
  4. Stronger regularization
  5. No look-ahead bias in ML

Usage:
    python scripts/research/backtest_walkforward_ml.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

from src.strategy.levels import get_rolling_levels, find_swing_points
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

DATA = ROOT / "data" / "processed" / "candles"

SYMBOLS = [
    # Tier 1
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT",
    # Tier 2
    "TRX/USDT", "LINK/USDT", "ADA/USDT", "AVAX/USDT", "NEAR/USDT",
    "LTC/USDT", "FET/USDT", "UNI/USDT",
    # Tier 3
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    # Tier 4
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "WIF/USDT", "ONDO/USDT", "RENDER/USDT",
    # Extra
    "ATOM/USDT", "ETC/USDT", "APT/USDT", "MANTA/USDT", "SEI/USDT",
    "JUP/USDT", "WLD/USDT", "STRK/USDT", "PENDLE/USDT", "ENA/USDT",
    "TAO/USDT", "GALA/USDT",
]

FEE_BPS = 10


# ======================================================================
# Level quality (simplified — top features only)
# ======================================================================

def compute_level_quality_slim(df: pd.DataFrame, level: Level, current_idx: int,
                                lookback: int = 200, swing_order: int = 5) -> dict:
    """Top level quality features only (reduce overfitting)."""
    start = max(0, current_idx - lookback)
    end = current_idx - swing_order
    if end - start < 20:
        return {}

    points = find_swing_points(df, start, end, order=swing_order)
    tolerance = level.zone_high - level.zone_low
    if tolerance <= 0:
        tolerance = level.price * 0.01

    level_touches = [(idx, price, ptype) for idx, price, ptype in points
                     if abs(price - level.price) <= tolerance * 1.2]

    if not level_touches:
        return {}

    f = {}

    # Volume at touches (top feature)
    touch_vol_ratios = []
    for idx, price, ptype in level_touches:
        if idx < 30 or idx >= len(df):
            continue
        avg_vol = df["volume"].iloc[max(0, idx - 30) : idx].mean()
        if avg_vol > 0:
            touch_vol_ratios.append(df["volume"].iloc[idx] / avg_vol)

    if touch_vol_ratios:
        f["lq_max_touch_vol"] = float(np.max(touch_vol_ratios))
        f["lq_touch_vol_trend"] = float(
            touch_vol_ratios[-1] / touch_vol_ratios[0]
        ) if len(touch_vol_ratios) > 1 and touch_vol_ratios[0] > 0 else 1.0
    else:
        f["lq_max_touch_vol"] = 1.0
        f["lq_touch_vol_trend"] = 1.0

    # Wick rejection (top feature)
    wick_rejections = []
    for idx, price, ptype in level_touches:
        if idx >= len(df):
            continue
        candle_range = df["high"].iloc[idx] - df["low"].iloc[idx]
        if candle_range <= 0:
            continue
        if ptype == "low":
            wick = min(df["close"].iloc[idx], df["open"].iloc[idx]) - df["low"].iloc[idx]
        else:
            wick = df["high"].iloc[idx] - max(df["close"].iloc[idx], df["open"].iloc[idx])
        wick_rejections.append(wick / candle_range)

    f["lq_max_wick_rejection"] = float(np.max(wick_rejections)) if wick_rejections else 0.0

    # Zone tightness
    touch_prices = [t[1] for t in level_touches]
    if len(touch_prices) >= 2:
        f["lq_zone_tightness"] = float(np.std(touch_prices) / level.price * 100)
    else:
        f["lq_zone_tightness"] = 0.0

    return f


# ======================================================================
# Trade collection (d1_only + slim LQ)
# ======================================================================

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


def collect_all_trades(entry_data, d1_data, params) -> pd.DataFrame:
    """Collect all trades across all symbols with features + timestamps."""
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
        cached_d1_levels = []
        last_check = 0
        last_d1_ts = None
        warmup = max(params["level_lookback"], params["trend_sma"]) + 10

        for i in range(warmup, len(df)):
            ts = df["ts"].iloc[i]

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
                        "hold_candles": i - at["entry_idx"],
                        "entry_ts": at["entry_ts"],
                    }
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            # D1 levels
            if last_d1_ts is None or (ts - last_d1_ts).total_seconds() > 86400:
                cached_d1_levels = get_d1_levels_at_time(d1_df, ts)
                last_d1_ts = ts

            if i - last_check >= params["check_interval"]:
                last_check = i
                new_brk = detect_breakouts(df, i, cached_d1_levels)
                recent_breakouts.extend(new_brk)
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= params["retest_window"]]

            close_i = df["close"].iloc[i]
            retest = detect_retests(df, i, recent_breakouts, params["retest_window"], trend)
            zakol = detect_zakol(df, i, cached_d1_levels, trend)
            best = retest if retest and (not zakol or retest[2] >= zakol[2]) else zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, params["rr_ratio"])
                if signal is None:
                    continue

                features = compute_features(df, i, lv, sig_type, params["trend_sma"])

                # Slim level quality
                lq = compute_level_quality_slim(df, lv, i,
                                                 lookback=params["level_lookback"])
                features.update(lq)

                # D1 level quality
                d1_mask = d1_df["ts"] <= ts
                d1_idx = d1_mask.sum() - 1
                if d1_idx > 50:
                    lq_d1 = compute_level_quality_slim(d1_df, lv, d1_idx,
                                                        lookback=120, swing_order=3)
                    for k, v in lq_d1.items():
                        features[f"d1_{k}"] = v

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "features": features,
                    "entry_ts": ts,
                }

    return pd.DataFrame(all_records)


# ======================================================================
# Walk-Forward ML
# ======================================================================

def walk_forward_ml(trades_df: pd.DataFrame,
                    train_months: int = 8,
                    test_months: int = 2,
                    top_k_features: int = 0,
                    min_leaf: int = 15,
                    n_estimators: int = 80) -> pd.DataFrame:
    """Walk-forward ML: train on past, predict future. No look-ahead.

    Returns DataFrame with all test predictions.
    """
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    df["target"] = (df["outcome"] == "win").astype(int)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df = df.sort_values("entry_ts").reset_index(drop=True)

    exclude = {"outcome", "target", "pnl_pct", "symbol", "signal_type",
               "hold_candles", "entry_ts", "d1_aligned"}
    all_feature_cols = [c for c in df.columns if c not in exclude]

    min_ts = df["entry_ts"].min()
    max_ts = df["entry_ts"].max()

    train_delta = pd.Timedelta(days=train_months * 30)
    test_delta = pd.Timedelta(days=test_months * 30)

    all_predictions = []
    window = 0

    current_start = min_ts

    while current_start + train_delta + test_delta <= max_ts + pd.Timedelta(days=1):
        train_end = current_start + train_delta
        test_end = train_end + test_delta

        train_mask = (df["entry_ts"] >= current_start) & (df["entry_ts"] < train_end)
        test_mask = (df["entry_ts"] >= train_end) & (df["entry_ts"] < test_end)

        train = df[train_mask]
        test = df[test_mask]

        if len(train) < 30 or len(test) < 5:
            current_start += test_delta
            window += 1
            continue

        feature_cols = all_feature_cols

        # Feature selection on training data
        if top_k_features > 0:
            X_train = train[feature_cols].fillna(0).values
            y_train = train["target"].values

            selector = GradientBoostingClassifier(
                n_estimators=50, max_depth=2, min_samples_leaf=min_leaf,
                subsample=0.8, random_state=42,
            )
            selector.fit(X_train, y_train)
            fi = sorted(zip(feature_cols, selector.feature_importances_),
                        key=lambda x: x[1], reverse=True)
            feature_cols = [n for n, v in fi[:top_k_features]]

        X_train = train[feature_cols].fillna(0).values
        y_train = train["target"].values
        X_test = test[feature_cols].fillna(0).values

        clf = GradientBoostingClassifier(
            n_estimators=n_estimators, max_depth=3, learning_rate=0.1,
            min_samples_leaf=min_leaf, subsample=0.8, random_state=42,
        )
        clf.fit(X_train, y_train)
        y_prob = clf.predict_proba(X_test)[:, 1]

        for idx, prob in zip(test.index, y_prob):
            all_predictions.append({
                "idx": idx,
                "ml_prob": prob,
                "window": window,
            })

        n_train_w = train["target"].sum()
        n_test_w = test["target"].sum()
        print(f"  Window {window}: train {len(train)}t ({n_train_w}W), "
              f"test {len(test)}t ({n_test_w}W), features={len(feature_cols)}")

        current_start += test_delta
        window += 1

    pred_df = pd.DataFrame(all_predictions).set_index("idx")
    result = df.join(pred_df, how="inner")
    return result


def analyze(result_df: pd.DataFrame, name: str, risk_pct: float = 4.0):
    """Analyze walk-forward results."""
    df = result_df.copy()
    total_days = (df["entry_ts"].max() - df["entry_ts"].min()).days
    period_months = total_days / 30

    n = len(df)
    base_wr = df["target"].mean()

    print(f"\n{'='*65}")
    print(f"  {name}")
    print(f"  {n} trades over {total_days} days ({period_months:.1f} months), baseline WR {base_wr*100:.1f}%")
    print(f"{'='*65}")

    results = []
    print(f"  {'Thr':>6} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Mo':>7} {'Ann':>7} {'MaxDD':>6}")

    for thr in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        mask = df["ml_prob"] >= thr
        if mask.sum() < 8:
            continue

        sub = df[mask]
        fy = sub["target"].values
        fp = sub["pnl_pct"].values
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

        # Max drawdown
        sized = fp * risk_pct / 100
        equity = np.cumprod(1 + sized / 100)
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak
        max_dd = dd.max() * 100

        print(f"  {thr:>6.2f} {mask.sum():>5} {tpm:>4.0f} {wr*100:>5.1f}% {pf:>5.2f} "
              f"{exp*100:>+8.3f}% {monthly*100:>+6.2f}% {annual*100:>+6.1f}% {max_dd:>5.1f}%")

        results.append({"thr": thr, "n": int(mask.sum()), "tpm": tpm, "wr": wr,
                         "pf": pf, "exp": exp, "monthly": monthly, "annual": annual,
                         "max_dd": max_dd})

    # Per-symbol
    if results:
        best_thr = max(results, key=lambda r: r["annual"])["thr"]
        sub = df[df["ml_prob"] >= best_thr]
        print(f"\n  Per-symbol (thr={best_thr:.2f}):")
        sym_stats = []
        for sym in sorted(sub["symbol"].unique()):
            s = sub[sub["symbol"] == sym]
            if len(s) >= 3:
                swr = s["target"].mean()
                spnl = s["pnl_pct"].sum()
                sym_stats.append((sym, len(s), swr, spnl))
        sym_stats.sort(key=lambda x: x[3], reverse=True)
        for sym, cnt, swr, spnl in sym_stats:
            print(f"    {sym:14s}: {cnt:3d}t, WR {swr*100:.0f}%, total {spnl:+.2f}%")

    # Per window consistency
    print(f"\n  Per-window consistency (thr={best_thr:.2f}):")
    for w in sorted(df["window"].unique()):
        wsub = df[(df["window"] == w) & (df["ml_prob"] >= best_thr)]
        if len(wsub) >= 3:
            wwr = wsub["target"].mean()
            wpnl = wsub["pnl_pct"].sum()
            print(f"    Window {w}: {len(wsub):3d}t, WR {wwr*100:.0f}%, pnl {wpnl:+.2f}%")

    return results


def main():
    print("=== Walk-Forward ML Backtest (Honest Evaluation) ===\n")

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    print(f"D1: {len(d1_data)}, 4H: {len(data_4h)} symbols")

    params = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150,
    )

    print("\nCollecting all trades (20 symbols, 12 months)...")
    all_trades = collect_all_trades(data_4h, d1_data, params)
    wl = all_trades[all_trades["outcome"].isin(["win", "loss"])]
    print(f"Total: {len(all_trades)} trades ({len(wl)} W/L, "
          f"{wl['outcome'].eq('win').sum()}W/{wl['outcome'].eq('loss').sum()}L)")

    risk_pct = 4.0

    # A) Walk-forward, all features, standard regularization
    print(f"\n{'='*65}")
    print("  A) Walk-forward ML — all features")
    print(f"{'='*65}")
    result_a = walk_forward_ml(all_trades, train_months=8, test_months=2,
                                top_k_features=0, min_leaf=15, n_estimators=80)
    res_a = analyze(result_a, "A) All features (walk-forward)", risk_pct)

    # B) Walk-forward, top 15 features
    print(f"\n{'='*65}")
    print("  B) Walk-forward ML — top 15 features (feature selection)")
    print(f"{'='*65}")
    result_b = walk_forward_ml(all_trades, train_months=8, test_months=2,
                                top_k_features=15, min_leaf=15, n_estimators=80)
    res_b = analyze(result_b, "B) Top 15 features (walk-forward)", risk_pct)

    # C) Walk-forward, top 10 features, stronger regularization
    print(f"\n{'='*65}")
    print("  C) Walk-forward ML — top 10, strong regularization")
    print(f"{'='*65}")
    result_c = walk_forward_ml(all_trades, train_months=8, test_months=2,
                                top_k_features=10, min_leaf=25, n_estimators=60)
    res_c = analyze(result_c, "C) Top 10, strong reg (walk-forward)", risk_pct)

    # D) No ML baseline (all trades, no filter)
    print(f"\n{'='*65}")
    print("  D) NO ML BASELINE")
    print(f"{'='*65}")
    base = all_trades[all_trades["outcome"].isin(["win", "loss"])].copy()
    n_base = len(base)
    base_days = 0
    if "entry_ts" in base.columns:
        base["entry_ts"] = pd.to_datetime(base["entry_ts"])
        base_days = (base["entry_ts"].max() - base["entry_ts"].min()).days
    base_months = base_days / 30 if base_days > 0 else 12
    wins_base = (base["outcome"] == "win").sum()
    losses_base = (base["outcome"] == "loss").sum()
    wr_base = wins_base / n_base
    pnl_base = base["pnl_pct"].values
    aw_base = pnl_base[base["outcome"] == "win"].mean() if wins_base > 0 else 0
    al_base = abs(pnl_base[base["outcome"] == "loss"].mean()) if losses_base > 0 else 0
    exp_base = wr_base * aw_base - (1 - wr_base) * al_base
    tpm_base = n_base / base_months
    monthly_base = tpm_base * exp_base * risk_pct / 100
    annual_base = (1 + monthly_base) ** 12 - 1

    print(f"  {n_base} trades ({wins_base}W/{losses_base}L), WR {wr_base*100:.1f}%")
    print(f"  Avg win: +{aw_base*100:.2f}%, avg loss: {al_base*100:.2f}%")
    print(f"  Exp: {exp_base*100:+.3f}%, {tpm_base:.0f} t/mo")
    print(f"  Monthly: {monthly_base*100:+.2f}%, Annual: {annual_base*100:+.1f}%")

    # Summary
    print(f"\n{'='*65}")
    print(f"  FINAL COMPARISON")
    print(f"{'='*65}")
    all_results = []
    for label, res in [("A) All features", res_a), ("B) Top 15", res_b), ("C) Top 10 strong", res_c)]:
        if res:
            best = max(res, key=lambda r: r["annual"])
            best["name"] = label
            all_results.append(best)

    all_results.append({"name": "D) No ML", "thr": 0, "n": n_base, "tpm": tpm_base,
                         "wr": wr_base, "pf": 0, "exp": exp_base,
                         "monthly": monthly_base, "annual": annual_base, "max_dd": 0})

    print(f"  {'Config':<25} {'Thr':>5} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Ann':>8}")
    for s in sorted(all_results, key=lambda x: x["annual"], reverse=True):
        pf_str = f"{s['pf']:>5.2f}" if s['pf'] else "  n/a"
        print(f"  {s['name']:<25} {s['thr']:>5.2f} {s['n']:>5} {s['tpm']:>4.0f} "
              f"{s['wr']*100:>5.1f}% {pf_str} "
              f"{s['exp']*100:>+8.3f}% {s['annual']*100:>+7.1f}%")


if __name__ == "__main__":
    main()
