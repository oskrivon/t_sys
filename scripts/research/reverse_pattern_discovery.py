"""
Reverse Pattern Discovery: analyze what precedes strong price moves.

Approach:
  1. Find all strong moves (>X% in N hours)
  2. Extract features from the period BEFORE each move
  3. Compare "before big move" vs "before nothing" — find discriminating features
  4. Cluster pre-move patterns to find archetypes
  5. Test: can we predict the next big move?

Usage:
    python scripts/research/reverse_pattern_discovery.py
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

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
    "AAVE/USDT", "UNI/USDT", "INJ/USDT", "TIA/USDT",
]


def fetch_ohlcv(symbol: str, tf: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c, cur = [], since_ms
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
            datasets[symbol] = pd.read_parquet(path)
            continue
        sys.stdout.write(f"  Downloading {symbol} {tf}...")
        sys.stdout.flush()
        df = fetch_ohlcv(symbol, tf, months)
        if not df.empty:
            df.to_parquet(path, index=False)
            sys.stdout.write(f" {len(df)} candles\n")
            datasets[symbol] = df
        else:
            sys.stdout.write(" FAILED\n")
    return datasets


# ======================================================================
# STEP 1: Find strong moves
# ======================================================================

def find_strong_moves(df: pd.DataFrame, symbol: str,
                      min_move_pct: float = 5.0,
                      window_candles: int = 6,  # 6h on 1h TF
                      cooldown: int = 12,
                      ) -> list[dict]:
    """Find all instances where price moved >min_move_pct% within window_candles."""
    moves = []
    last_move_idx = -cooldown

    for i in range(window_candles, len(df) - window_candles):
        if i - last_move_idx < cooldown:
            continue

        # Forward return over window
        entry = df["close"].iloc[i]
        future_high = df["high"].iloc[i+1 : i+1+window_candles].max()
        future_low = df["low"].iloc[i+1 : i+1+window_candles].min()

        up_move = (future_high - entry) / entry * 100
        down_move = (entry - future_low) / entry * 100

        if up_move >= min_move_pct:
            moves.append({
                "symbol": symbol, "idx": i, "direction": "up",
                "move_pct": up_move, "ts": df["ts"].iloc[i],
            })
            last_move_idx = i
        elif down_move >= min_move_pct:
            moves.append({
                "symbol": symbol, "idx": i, "direction": "down",
                "move_pct": down_move, "ts": df["ts"].iloc[i],
            })
            last_move_idx = i

    return moves


# ======================================================================
# STEP 2: Extract pre-move features
# ======================================================================

def extract_pre_move_features(df: pd.DataFrame, idx: int, lookback: int = 24) -> dict:
    """Extract features from the period before a move.

    Lookback is in candles (24 candles on 1h = 24 hours).
    """
    if idx < lookback + 10:
        return {}

    f = {}
    window = df.iloc[idx - lookback : idx]
    close = df["close"].iloc[idx]

    # -- Price action --
    f["return_24h"] = (close / df["close"].iloc[idx - lookback] - 1) * 100
    f["return_12h"] = (close / df["close"].iloc[idx - lookback//2] - 1) * 100
    f["return_6h"] = (close / df["close"].iloc[idx - lookback//4] - 1) * 100

    # Absolute moves (direction-agnostic)
    f["abs_return_24h"] = abs(f["return_24h"])
    f["abs_return_12h"] = abs(f["return_12h"])

    # -- Volatility --
    returns = window["close"].pct_change().dropna()
    f["volatility"] = float(returns.std() * 100) if len(returns) > 1 else 0
    f["range_pct"] = (window["high"].max() - window["low"].min()) / close * 100

    # ATR
    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - window["close"].shift(1)).abs(),
        (window["low"] - window["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    f["atr_pct"] = float(tr.mean() / close * 100)

    # -- Volume --
    avg_vol = df["volume"].iloc[max(0, idx-lookback*3) : idx-lookback].mean()
    recent_vol = window["volume"].mean()
    f["volume_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0

    # Volume spike (max vol in window vs average)
    f["volume_spike"] = float(window["volume"].max() / avg_vol) if avg_vol > 0 else 1.0

    # Volume trend (last 6h vs first 18h of window)
    split = lookback // 4
    vol_recent = window["volume"].iloc[-split:].mean()
    vol_earlier = window["volume"].iloc[:-split].mean()
    f["volume_trend"] = float(vol_recent / vol_earlier) if vol_earlier > 0 else 1.0

    # -- Candle patterns --
    greens = (window["close"] > window["open"]).sum()
    f["green_ratio"] = greens / len(window)

    # Body sizes
    bodies = (window["close"] - window["open"]).abs()
    wicks_upper = window["high"] - window[["close", "open"]].max(axis=1)
    wicks_lower = window[["close", "open"]].min(axis=1) - window["low"]

    f["avg_body_pct"] = float((bodies / close).mean() * 100)
    f["avg_upper_wick_pct"] = float((wicks_upper / close).mean() * 100)
    f["avg_lower_wick_pct"] = float((wicks_lower / close).mean() * 100)

    # Last few candles pattern
    last3 = df.iloc[idx-3 : idx]
    f["last3_return"] = (df["close"].iloc[idx-1] / df["close"].iloc[idx-4] - 1) * 100
    f["last3_vol_ratio"] = float(last3["volume"].mean() / avg_vol) if avg_vol > 0 else 1.0

    # -- Trend context --
    sma_20 = df["close"].iloc[max(0, idx-20):idx].mean()
    sma_50 = df["close"].iloc[max(0, idx-50):idx].mean()
    f["dist_sma20_pct"] = (close - sma_20) / sma_20 * 100
    f["dist_sma50_pct"] = (close - sma_50) / sma_50 * 100

    # Trend direction
    if idx >= 20:
        f["sma20_slope"] = (sma_20 - df["close"].iloc[max(0, idx-25):max(1, idx-5)].mean()) / close * 100
    else:
        f["sma20_slope"] = 0

    # -- RSI --
    if idx >= 14:
        deltas = df["close"].iloc[idx-14:idx+1].diff().dropna()
        gains = deltas.clip(lower=0).mean()
        losses_v = (-deltas.clip(upper=0)).mean()
        rs = gains / losses_v if losses_v > 0 else 100
        f["rsi"] = 100 - (100 / (1 + rs))
    else:
        f["rsi"] = 50

    # -- Consolidation detection --
    # Range getting tighter? (Bollinger bandwidth shrinking)
    if idx >= 20:
        std_20 = df["close"].iloc[idx-20:idx].std()
        std_prev = df["close"].iloc[idx-40:idx-20].std() if idx >= 40 else std_20
        f["volatility_contraction"] = float(std_20 / std_prev) if std_prev > 0 else 1.0
    else:
        f["volatility_contraction"] = 1.0

    # Flat range detection: how many times did price touch extremes?
    range_high = window["high"].max()
    range_low = window["low"].min()
    range_mid = (range_high + range_low) / 2
    near_high = (window["high"] > range_high * 0.995).sum()
    near_low = (window["low"] < range_low * 1.005).sum()
    f["touches_high"] = near_high
    f["touches_low"] = near_low
    f["is_range_bound"] = 1.0 if near_high >= 2 and near_low >= 2 else 0.0

    # -- Time features --
    ts = df["ts"].iloc[idx]
    if hasattr(ts, "hour"):
        f["hour_utc"] = float(ts.hour)
        f["day_of_week"] = float(ts.dayofweek)
    else:
        f["hour_utc"] = 12.0
        f["day_of_week"] = 3.0

    return f


# ======================================================================
# STEP 3: Compare big moves vs random (no-move) periods
# ======================================================================

def collect_dataset(datasets, min_move_pct=5.0, window=6, lookback=24):
    """Collect labeled dataset: big_move vs no_move."""
    all_records = []

    for symbol, df in datasets.items():
        # Find big moves
        moves = find_strong_moves(df, symbol, min_move_pct, window, cooldown=lookback)

        move_indices = set(m["idx"] for m in moves)

        # Extract features for big moves
        for m in moves:
            features = extract_pre_move_features(df, m["idx"], lookback)
            if features:
                features["label"] = 1  # big move
                features["direction"] = m["direction"]
                features["move_pct"] = m["move_pct"]
                features["symbol"] = symbol
                all_records.append(features)

        # Sample "no move" periods (same count, random)
        no_move_candidates = [
            i for i in range(lookback + 10, len(df) - window)
            if i not in move_indices
            and all(abs(i - mi) > lookback for mi in move_indices)
        ]

        np.random.seed(42)
        n_sample = min(len(moves) * 2, len(no_move_candidates))
        if n_sample > 0:
            sampled = np.random.choice(no_move_candidates, n_sample, replace=False)
            for idx in sampled:
                features = extract_pre_move_features(df, idx, lookback)
                if features:
                    features["label"] = 0  # no move
                    features["direction"] = "none"
                    features["move_pct"] = 0
                    features["symbol"] = symbol
                    all_records.append(features)

    return pd.DataFrame(all_records)


# ======================================================================
# STEP 4: Analyze and find discriminating features
# ======================================================================

def analyze_features(data: pd.DataFrame):
    """Compare feature distributions between big-move and no-move."""
    feature_cols = [c for c in data.columns
                    if c not in ["label", "direction", "move_pct", "symbol"]]

    moves = data[data["label"] == 1]
    no_moves = data[data["label"] == 0]

    print(f"\n  Feature comparison: big_move ({len(moves)}) vs no_move ({len(no_moves)})")
    print(f"  {'Feature':<25} {'Move':>8} {'NoMove':>8} {'Diff':>8} {'Ratio':>8}")

    diffs = []
    for col in feature_cols:
        m_mean = moves[col].mean()
        n_mean = no_moves[col].mean()
        m_std = moves[col].std()
        n_std = no_moves[col].std()

        # Effect size (Cohen's d)
        pooled_std = np.sqrt((m_std**2 + n_std**2) / 2) if (m_std + n_std) > 0 else 1
        d = (m_mean - n_mean) / pooled_std if pooled_std > 0 else 0

        ratio = m_mean / n_mean if abs(n_mean) > 0.001 else 0

        diffs.append((col, m_mean, n_mean, d, ratio))

    # Sort by absolute effect size
    diffs.sort(key=lambda x: abs(x[3]), reverse=True)

    for col, m_mean, n_mean, d, ratio in diffs[:20]:
        print(f"  {col:<25} {m_mean:>8.3f} {n_mean:>8.3f} {d:>+7.2f}d {ratio:>7.2f}x")

    return diffs


def train_predictor(data: pd.DataFrame):
    """Train ML to predict big moves from pre-move features."""
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.model_selection import cross_val_predict
    from sklearn.metrics import classification_report

    feature_cols = [c for c in data.columns
                    if c not in ["label", "direction", "move_pct", "symbol"]]

    X = data[feature_cols].fillna(0).values
    y = data["label"].values

    print(f"\n  ML Prediction: {len(data)} samples ({y.sum()} moves, {len(y)-y.sum()} no-move)")

    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        min_samples_leaf=10, subsample=0.8, random_state=42,
    )

    y_prob = cross_val_predict(clf, X, y, cv=5, method="predict_proba")[:, 1]

    print(f"\n  {'Thr':>6} {'Predicted':>10} {'Actual':>8} {'Precision':>10} {'Recall':>8}")
    for thr in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        pred = y_prob >= thr
        if pred.sum() == 0:
            continue
        tp = (pred & (y == 1)).sum()
        fp = (pred & (y == 0)).sum()
        fn = (~pred & (y == 1)).sum()
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        print(f"  {thr:>6.1f} {pred.sum():>10} {tp:>8} {precision:>9.1%} {recall:>7.1%}")

    # Feature importance
    clf.fit(X, y)
    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Feature importance:")
    for name, imp in fi[:15]:
        bar = "#" * int(imp * 150)
        print(f"    {name:<25} {imp:.4f} {bar}")

    # Direction analysis: what predicts UP vs DOWN?
    up_moves = data[(data["label"] == 1) & (data["direction"] == "up")]
    dn_moves = data[(data["label"] == 1) & (data["direction"] == "down")]
    if len(up_moves) > 10 and len(dn_moves) > 10:
        print(f"\n  Direction discriminators (UP vs DOWN moves):")
        print(f"  {'Feature':<25} {'UP':>8} {'DOWN':>8} {'Delta':>8}")
        for col in feature_cols:
            u = up_moves[col].mean()
            d = dn_moves[col].mean()
            delta = u - d
            if abs(delta) > 0.3:
                print(f"  {col:<25} {u:>8.3f} {d:>8.3f} {delta:>+7.3f}")

    return clf, feature_cols, y_prob


def analyze_by_symbol(data: pd.DataFrame, y_prob: np.ndarray):
    """Analyze which symbols have the most predictable moves."""
    data = data.copy()
    data["prob"] = y_prob

    print(f"\n  Per-symbol predictability:")
    print(f"  {'Symbol':<12} {'Moves':>6} {'NoMove':>7} {'AvgProb(M)':>11} {'AvgProb(N)':>11} {'AUC-like':>9}")

    for sym in sorted(data["symbol"].unique()):
        sub = data[data["symbol"] == sym]
        moves = sub[sub["label"] == 1]
        no_moves = sub[sub["label"] == 0]
        if len(moves) < 5:
            continue
        mp = moves["prob"].mean()
        np_ = no_moves["prob"].mean()
        # Rough AUC approximation
        auc = (mp - np_) / 2 + 0.5 if len(no_moves) > 0 else 0.5
        print(f"  {sym:<12} {len(moves):>6} {len(no_moves):>7} {mp:>10.3f} {np_:>10.3f} {auc:>8.3f}")


def analyze_move_sizes(data: pd.DataFrame):
    """Break down by move size to find sweet spots."""
    moves = data[data["label"] == 1].copy()
    if moves.empty:
        return

    print(f"\n  Move size distribution:")
    print(f"  {'Range':>12} {'Count':>6} {'Up':>5} {'Down':>5} {'Avg Vol Ratio':>14} {'Avg Volatility':>15}")
    for lo, hi in [(5, 7), (7, 10), (10, 15), (15, 25), (25, 100)]:
        sub = moves[(moves["move_pct"] >= lo) & (moves["move_pct"] < hi)]
        if len(sub) < 3:
            continue
        up = (sub["direction"] == "up").sum()
        dn = (sub["direction"] == "down").sum()
        vr = sub["volume_ratio"].mean()
        vol = sub["volatility"].mean()
        print(f"  {lo:>4}-{hi:<4}%   {len(sub):>6} {up:>5} {dn:>5} {vr:>13.2f} {vol:>14.3f}")


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    print("=== Reverse Pattern Discovery ===\n")

    # Use 1h data for granularity
    print("Loading 1h data (6 months)...")
    datasets = load_data("1h", 6)
    print(f"  {len(datasets)} symbols loaded\n")

    # Test multiple thresholds
    for move_threshold in [5.0, 8.0, 12.0]:
        print(f"\n{'='*70}")
        print(f"  THRESHOLD: >={move_threshold}% move in 6 hours")
        print(f"{'='*70}")

        # Count moves per symbol
        total_moves = 0
        for symbol, df in datasets.items():
            moves = find_strong_moves(df, symbol, move_threshold, window_candles=6)
            up = len([m for m in moves if m["direction"] == "up"])
            dn = len([m for m in moves if m["direction"] == "down"])
            if moves:
                print(f"  {symbol:12s}: {len(moves):3d} moves ({up} up, {dn} down)")
            total_moves += len(moves)

        print(f"  Total: {total_moves} moves across {len(datasets)} symbols")

        if total_moves < 30:
            print("  Not enough moves for analysis!")
            continue

        # Collect dataset
        print(f"\n  Collecting features (lookback=24h)...")
        data = collect_dataset(datasets, move_threshold, window=6, lookback=24)
        print(f"  Dataset: {len(data)} samples")

        # Analyze
        analyze_features(data)
        analyze_move_sizes(data)
        clf, feat_cols, y_prob = train_predictor(data)
        analyze_by_symbol(data, y_prob)


if __name__ == "__main__":
    main()
