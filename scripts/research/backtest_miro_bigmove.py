"""
Backtest: Miro Strategy + Big Move Detector combined.

Hypothesis: Miro signal (direction via levels) + Big Move (timing) = WR 50%+.

Approach:
  - Use d1_only Miro pipeline (best: 13.3% annual)
  - At each Miro signal, compute P(big_move) from 1h data
  - Test: Miro + big_move_prob as additional filter/feature
  - Walk-forward ML with big_move features added

Usage:
    python scripts/research/backtest_miro_bigmove.py
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
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import cross_val_predict

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


# ======================================================================
# Big Move features (from reverse_pattern_discovery.py, adapted for 4h)
# ======================================================================

def compute_bigmove_features(df_1h: pd.DataFrame, target_ts, lookback: int = 24) -> dict:
    """Compute big-move features from 1h data at given timestamp.

    Returns dict of bm_* features, or empty dict if not enough data.
    """
    # Find the closest 1h candle to target_ts
    if df_1h is None or df_1h.empty:
        return {}

    mask = df_1h["ts"] <= target_ts
    if mask.sum() < lookback + 10:
        return {}

    idx = mask.sum() - 1
    window = df_1h.iloc[idx - lookback : idx]
    close = df_1h["close"].iloc[idx]

    f = {}

    # Volatility
    returns = window["close"].pct_change().dropna()
    f["bm_volatility"] = float(returns.std() * 100) if len(returns) > 1 else 0
    f["bm_range_pct"] = (window["high"].max() - window["low"].min()) / close * 100

    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - window["close"].shift(1)).abs(),
        (window["low"] - window["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    f["bm_atr_pct"] = float(tr.mean() / close * 100)

    # Volume
    avg_vol = df_1h["volume"].iloc[max(0, idx - lookback * 3) : idx - lookback].mean()
    recent_vol = window["volume"].mean()
    f["bm_volume_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0
    f["bm_volume_spike"] = float(window["volume"].max() / avg_vol) if avg_vol > 0 else 1.0

    split = lookback // 4
    vol_recent = window["volume"].iloc[-split:].mean()
    vol_earlier = window["volume"].iloc[:-split].mean()
    f["bm_volume_trend"] = float(vol_recent / vol_earlier) if vol_earlier > 0 else 1.0

    # Candle patterns
    bodies = (window["close"] - window["open"]).abs()
    wicks_lower = window[["close", "open"]].min(axis=1) - window["low"]
    f["bm_avg_lower_wick_pct"] = float((wicks_lower / close).mean() * 100)

    # Volatility contraction (squeeze)
    if idx >= 40:
        std_20 = df_1h["close"].iloc[idx - 20 : idx].std()
        std_prev = df_1h["close"].iloc[idx - 40 : idx - 20].std()
        f["bm_vol_contraction"] = float(std_20 / std_prev) if std_prev > 0 else 1.0
    else:
        f["bm_vol_contraction"] = 1.0

    # Range-bound detection
    range_high = window["high"].max()
    range_low = window["low"].min()
    near_high = (window["high"] > range_high * 0.995).sum()
    near_low = (window["low"] < range_low * 1.005).sum()
    f["bm_is_range_bound"] = 1.0 if near_high >= 2 and near_low >= 2 else 0.0

    # Time
    ts = df_1h["ts"].iloc[idx]
    if hasattr(ts, "hour"):
        f["bm_hour_utc"] = float(ts.hour)
    else:
        f["bm_hour_utc"] = 12.0

    return f


# ======================================================================
# Big Move probability model (trained on 1h data, walk-forward)
# ======================================================================

class BigMoveModel:
    """Walk-forward big move probability model."""

    def __init__(self, datasets_1h: dict[str, pd.DataFrame],
                 min_move_pct: float = 5.0, move_window: int = 6):
        self.datasets_1h = datasets_1h
        self.min_move_pct = min_move_pct
        self.move_window = move_window
        self.clf = None
        self.feature_cols = None
        self._last_train_ts = None

    def _extract_full_features(self, df: pd.DataFrame, idx: int, lookback: int = 24) -> dict:
        """Full feature set for big move prediction (same as reverse_pattern_discovery)."""
        if idx < lookback + 10:
            return {}

        f = {}
        window = df.iloc[idx - lookback : idx]
        close = df["close"].iloc[idx]

        f["return_24h"] = (close / df["close"].iloc[idx - lookback] - 1) * 100
        f["return_12h"] = (close / df["close"].iloc[idx - lookback // 2] - 1) * 100
        f["return_6h"] = (close / df["close"].iloc[idx - lookback // 4] - 1) * 100
        f["abs_return_24h"] = abs(f["return_24h"])
        f["abs_return_12h"] = abs(f["return_12h"])

        returns = window["close"].pct_change().dropna()
        f["volatility"] = float(returns.std() * 100) if len(returns) > 1 else 0
        f["range_pct"] = (window["high"].max() - window["low"].min()) / close * 100

        tr = pd.concat([
            window["high"] - window["low"],
            (window["high"] - window["close"].shift(1)).abs(),
            (window["low"] - window["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        f["atr_pct"] = float(tr.mean() / close * 100)

        avg_vol = df["volume"].iloc[max(0, idx - lookback * 3) : idx - lookback].mean()
        recent_vol = window["volume"].mean()
        f["volume_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0
        f["volume_spike"] = float(window["volume"].max() / avg_vol) if avg_vol > 0 else 1.0

        split = lookback // 4
        vol_recent = window["volume"].iloc[-split:].mean()
        vol_earlier = window["volume"].iloc[:-split].mean()
        f["volume_trend"] = float(vol_recent / vol_earlier) if vol_earlier > 0 else 1.0

        greens = (window["close"] > window["open"]).sum()
        f["green_ratio"] = greens / len(window)

        bodies = (window["close"] - window["open"]).abs()
        wicks_upper = window["high"] - window[["close", "open"]].max(axis=1)
        wicks_lower = window[["close", "open"]].min(axis=1) - window["low"]
        f["avg_body_pct"] = float((bodies / close).mean() * 100)
        f["avg_upper_wick_pct"] = float((wicks_upper / close).mean() * 100)
        f["avg_lower_wick_pct"] = float((wicks_lower / close).mean() * 100)

        f["last3_return"] = (df["close"].iloc[idx - 1] / df["close"].iloc[idx - 4] - 1) * 100
        last3 = df.iloc[idx - 3 : idx]
        f["last3_vol_ratio"] = float(last3["volume"].mean() / avg_vol) if avg_vol > 0 else 1.0

        sma_20 = df["close"].iloc[max(0, idx - 20) : idx].mean()
        sma_50 = df["close"].iloc[max(0, idx - 50) : idx].mean()
        f["dist_sma20_pct"] = (close - sma_20) / sma_20 * 100
        f["dist_sma50_pct"] = (close - sma_50) / sma_50 * 100

        if idx >= 20:
            f["sma20_slope"] = (sma_20 - df["close"].iloc[max(0, idx - 25) : max(1, idx - 5)].mean()) / close * 100
        else:
            f["sma20_slope"] = 0

        if idx >= 14:
            deltas = df["close"].iloc[idx - 14 : idx + 1].diff().dropna()
            gains = deltas.clip(lower=0).mean()
            losses_v = (-deltas.clip(upper=0)).mean()
            rs = gains / losses_v if losses_v > 0 else 100
            f["rsi"] = 100 - (100 / (1 + rs))
        else:
            f["rsi"] = 50

        if idx >= 40:
            std_20 = df["close"].iloc[idx - 20 : idx].std()
            std_prev = df["close"].iloc[idx - 40 : idx - 20].std()
            f["volatility_contraction"] = float(std_20 / std_prev) if std_prev > 0 else 1.0
        else:
            f["volatility_contraction"] = 1.0

        range_high = window["high"].max()
        range_low = window["low"].min()
        near_high = (window["high"] > range_high * 0.995).sum()
        near_low = (window["low"] < range_low * 1.005).sum()
        f["touches_high"] = near_high
        f["touches_low"] = near_low
        f["is_range_bound"] = 1.0 if near_high >= 2 and near_low >= 2 else 0.0

        ts = df["ts"].iloc[idx]
        if hasattr(ts, "hour"):
            f["hour_utc"] = float(ts.hour)
            f["day_of_week"] = float(ts.dayofweek)
        else:
            f["hour_utc"] = 12.0
            f["day_of_week"] = 3.0

        return f

    def train(self, up_to_ts):
        """Train model on all 1h data up to given timestamp."""
        train_X, train_y = [], []

        for symbol, df in self.datasets_1h.items():
            sub = df[df["ts"] <= up_to_ts]
            if len(sub) < 100:
                continue

            # Find big moves
            for i in range(34, len(sub) - self.move_window):
                entry = sub["close"].iloc[i]
                fh = sub["high"].iloc[i + 1 : i + 1 + self.move_window].max()
                fl = sub["low"].iloc[i + 1 : i + 1 + self.move_window].min()
                up = (fh - entry) / entry * 100
                down = (entry - fl) / entry * 100
                is_move = 1 if (up >= self.min_move_pct or down >= self.min_move_pct) else 0

                # Sample: take all moves, subsample non-moves
                if is_move == 0 and np.random.random() > 0.05:  # ~5% of non-moves
                    continue

                feat = self._extract_full_features(sub, i)
                if feat:
                    if self.feature_cols is None:
                        self.feature_cols = sorted(feat.keys())
                    train_X.append([feat.get(c, 0) for c in self.feature_cols])
                    train_y.append(is_move)

        if len(train_X) < 50:
            return False

        X = np.array(train_X)
        y = np.array(train_y)

        self.clf = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            min_samples_leaf=max(5, len(X) // 50),
            subsample=0.8, random_state=42,
        )
        self.clf.fit(X, y)
        self._last_train_ts = up_to_ts
        return True

    def predict(self, df_1h: pd.DataFrame, target_ts) -> float:
        """Get P(big_move) at target_ts using 1h data."""
        if self.clf is None or self.feature_cols is None:
            return 0.0

        mask = df_1h["ts"] <= target_ts
        if mask.sum() < 34:
            return 0.0

        idx = mask.sum() - 1
        feat = self._extract_full_features(df_1h, idx)
        if not feat:
            return 0.0

        x = np.array([[feat.get(c, 0) for c in self.feature_cols]])
        return float(self.clf.predict_proba(x)[0][1])


# ======================================================================
# Miro backtest with Big Move integration
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


def collect_trades(entry_data, d1_data, data_1h, bigmove_model, params):
    """Walk-forward: collect Miro trades with Miro features + big_move features."""
    all_records = []
    retrain_interval_days = 30
    last_train_date = None

    for symbol in SYMBOLS:
        if symbol not in entry_data or symbol not in d1_data:
            continue
        df = entry_data[symbol]
        d1_df = d1_data[symbol]
        df_1h = data_1h.get(symbol)

        sma = df["close"].rolling(params["trend_sma"]).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts: list[Breakout] = []
        cached_d1_levels = []
        cached_levels = []
        last_check = 0
        last_d1_ts = None
        warmup = max(params["level_lookback"], params["trend_sma"]) + 10

        for i in range(warmup, len(df)):
            ts = df["ts"].iloc[i]

            # Retrain big move model periodically
            if bigmove_model and (last_train_date is None or
                    (ts - last_train_date).days >= retrain_interval_days):
                # Train on first symbol pass only
                if symbol == SYMBOLS[0]:
                    bigmove_model.train(ts)
                last_train_date = ts

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
                        "bigmove_prob": at.get("bigmove_prob", 0.0),
                    }
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            # D1 levels
            if last_d1_ts is None or (ts - last_d1_ts).total_seconds() > 86400:
                cached_d1_levels = get_d1_levels_at_time(d1_df, ts)
                last_d1_ts = ts

            # Use d1_only mode (best Miro config)
            if i - last_check >= params["check_interval"]:
                cached_levels = cached_d1_levels
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

                # Miro features
                features = compute_features(df, i, lv, sig_type, params["trend_sma"])
                features["d1_aligned"] = 1.0

                # Big move features from 1h data
                bigmove_prob = 0.0
                if bigmove_model and df_1h is not None:
                    bigmove_prob = bigmove_model.predict(df_1h, ts)
                    bm_feats = compute_bigmove_features(df_1h, ts)
                    features.update(bm_feats)
                features["bigmove_prob"] = bigmove_prob

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "features": features,
                    "bigmove_prob": bigmove_prob,
                }

    return pd.DataFrame(all_records)


def run_ml_analysis(trades_df, name, risk_pct, hpc, period_months,
                    bigmove_filter=None):
    """Run ML analysis, optionally filtering by bigmove_prob first."""
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()

    # Apply big move filter if specified
    filter_label = ""
    if bigmove_filter is not None and "bigmove_prob" in df.columns:
        before = len(df)
        df = df[df["bigmove_prob"] >= bigmove_filter]
        filter_label = f" [bm>={bigmove_filter}]"
        print(f"    Big move filter {bigmove_filter}: {before} -> {len(df)} trades")

    if len(df) < 20:
        print(f"  {name}{filter_label}: Too few trades ({len(df)})")
        return []

    df["target"] = (df["outcome"] == "win").astype(int)

    # Exclude meta columns from features
    exclude = {"outcome", "target", "pnl_pct", "symbol", "signal_type",
               "hold_candles", "bigmove_prob", "d1_aligned"}
    feature_cols = [c for c in df.columns if c not in exclude]

    X = df[feature_cols].fillna(0).values
    y = df["target"].values

    print(f"\n  {name}{filter_label}: {len(df)} trades ({y.sum()}W/{len(y)-y.sum()}L), "
          f"baseline WR {y.mean()*100:.1f}%")

    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        min_samples_leaf=max(5, len(df) // 50), subsample=0.8, random_state=42,
    )

    cv = min(5, max(2, len(df) // 30))
    y_prob = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")[:, 1]

    results = []
    print(f"  {'Thr':>6} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Mo':>7} {'Ann':>7}")
    for thr in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        mask = y_prob >= thr
        if mask.sum() < 8:
            continue
        fy = y[mask]
        fp = df["pnl_pct"].values[mask]
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

        print(f"  {thr:>6.2f} {mask.sum():>5} {tpm:>4.0f} {wr*100:>5.1f}% {pf:>5.2f} "
              f"{exp*100:>+8.3f}% {monthly*100:>+6.2f}% {annual*100:>+6.1f}%")

        results.append({"thr": thr, "n": int(mask.sum()), "tpm": tpm, "wr": wr,
                         "pf": pf, "exp": exp, "monthly": monthly, "annual": annual,
                         "filter": filter_label})

    # Feature importance
    clf.fit(X, y)
    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top features: ", end="")
    print(", ".join(f"{n}={v:.3f}" for n, v in fi[:10]))

    # Show bm_ features importance
    bm_fi = [(n, v) for n, v in fi if n.startswith("bm_")]
    if bm_fi:
        print(f"  Big Move features: ", end="")
        print(", ".join(f"{n}={v:.3f}" for n, v in bm_fi))

    return results


def main():
    print("=== Miro + Big Move Detector Combined Backtest ===\n")

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    data_1h = load_data("1h")

    print(f"D1: {len(d1_data)}, 4H: {len(data_4h)}, 1H: {len(data_1h)} symbols")

    if not data_4h:
        print("No 4h data found!")
        return

    period_months = 6.8
    risk_pct = 3.0
    hpc = 4  # hours per candle

    params = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150,
    )

    # ============================================================
    # A) Baseline: Miro d1_only WITHOUT big move (control)
    # ============================================================
    print("\n" + "=" * 70)
    print("  A) BASELINE: Miro d1_only (no big move)")
    print("=" * 70)

    print("  Collecting trades...")
    baseline_df = collect_trades(data_4h, d1_data, {}, None, params)
    print(f"  Total: {len(baseline_df)} trades")

    baseline_results = run_ml_analysis(baseline_df, "Miro d1_only baseline",
                                        risk_pct, hpc, period_months)

    # ============================================================
    # B) Miro + Big Move features in ML (features enriched)
    # ============================================================
    print("\n" + "=" * 70)
    print("  B) COMBINED: Miro d1_only + Big Move features in ML")
    print("=" * 70)

    print("  Training big move model...")
    np.random.seed(42)
    bm_model = BigMoveModel(data_1h, min_move_pct=5.0, move_window=6)

    print("  Collecting trades with big move features...")
    combined_df = collect_trades(data_4h, d1_data, data_1h, bm_model, params)
    print(f"  Total: {len(combined_df)} trades")

    # B1: ML with all features (Miro + big move)
    combined_results = run_ml_analysis(combined_df, "Miro + BM features",
                                        risk_pct, hpc, period_months)

    # ============================================================
    # C) Miro + Big Move as pre-filter (only trade when P(bm) > thr)
    # ============================================================
    print("\n" + "=" * 70)
    print("  C) FILTERED: Miro d1_only + Big Move pre-filter")
    print("=" * 70)

    filter_results = {}
    for bm_thr in [0.3, 0.4, 0.5, 0.6, 0.7]:
        results = run_ml_analysis(combined_df, f"Miro + BM filter",
                                   risk_pct, hpc, period_months,
                                   bigmove_filter=bm_thr)
        if results:
            best = max(results, key=lambda r: r["annual"])
            filter_results[bm_thr] = best

    # ============================================================
    # SUMMARY
    # ============================================================
    print(f"\n{'='*70}")
    print(f"  FINAL COMPARISON")
    print(f"{'='*70}")

    all_best = []

    if baseline_results:
        best_base = max(baseline_results, key=lambda r: r["annual"])
        best_base["name"] = "A) Miro baseline"
        all_best.append(best_base)

    if combined_results:
        best_comb = max(combined_results, key=lambda r: r["annual"])
        best_comb["name"] = "B) Miro + BM features"
        all_best.append(best_comb)

    for bm_thr, best_f in filter_results.items():
        best_f["name"] = f"C) Miro + BM>={bm_thr}"
        all_best.append(best_f)

    print(f"  {'Config':<28} {'Thr':>5} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Annual':>8}")
    for s in sorted(all_best, key=lambda x: x["annual"], reverse=True):
        print(f"  {s['name']:<28} {s['thr']:>5.2f} {s['n']:>5} {s['tpm']:>4.0f} "
              f"{s['wr']*100:>5.1f}% {s['pf']:>5.2f} "
              f"{s['exp']*100:>+8.3f}% {s['annual']*100:>+7.1f}%")

    # Highlight improvement
    if len(all_best) >= 2:
        base_ann = all_best[-1]["annual"] if all_best[-1]["name"].startswith("A)") else 0
        best_overall = all_best[0]
        if base_ann != 0:
            delta = (best_overall["annual"] - base_ann) * 100
            print(f"\n  Delta vs baseline: {delta:+.1f}pp annual")


if __name__ == "__main__":
    main()
