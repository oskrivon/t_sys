"""Big Move Detector: denoised edition.

Apply signal processing filters to price data before feature extraction:
  1. Wavelet denoising — remove high-freq noise from close/volume
  2. Kalman filter — adaptive trend estimate (replaces RSI for direction)
  3. Both combined

Compare walk-forward backtest results with baseline (raw data + RSI).

Usage:
    python scripts/research/bigmove_denoised.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pywt
from filterpy.kalman import KalmanFilter
from sklearn.ensemble import GradientBoostingClassifier

ROOT = Path("/root/trading")
DATA = ROOT / "data" / "processed" / "candles"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "DOGE/USDT", "SUI/USDT", "AVAX/USDT",
    "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10
MOVE_PCT = 5.0
MOVE_WINDOW = 6
TRAIN_CANDLES = 24 * 90
TEST_CANDLES = 24 * 30
STEP_CANDLES = 24 * 30
MAX_HOLD = 12
SL_PCT = 2.5
RR = 2.0


# ======================================================================
# Signal processing filters
# ======================================================================

def wavelet_denoise(series: np.ndarray, wavelet: str = "db4",
                     level: int = 3, mode: str = "soft") -> np.ndarray:
    """Wavelet denoising using universal threshold."""
    coeffs = pywt.wavedec(series, wavelet, level=level)
    # Universal threshold (VisuShrink)
    sigma = np.median(np.abs(coeffs[-1])) / 0.6745
    threshold = sigma * np.sqrt(2 * np.log(len(series)))
    # Threshold detail coefficients (keep approximation)
    denoised_coeffs = [coeffs[0]]  # keep approx
    for c in coeffs[1:]:
        if mode == "soft":
            denoised_coeffs.append(pywt.threshold(c, threshold, mode="soft"))
        else:
            denoised_coeffs.append(pywt.threshold(c, threshold, mode="hard"))
    return pywt.waverec(denoised_coeffs, wavelet)[:len(series)]


def kalman_smooth(series: np.ndarray, R: float = 1.0, Q: float = 0.01) -> np.ndarray:
    """1D Kalman filter for price smoothing.
    R = measurement noise, Q = process noise.
    Higher R/Q ratio = smoother output."""
    kf = KalmanFilter(dim_x=2, dim_z=1)
    # State: [price, velocity]
    kf.F = np.array([[1, 1], [0, 1]])  # state transition
    kf.H = np.array([[1, 0]])          # measurement
    kf.R = np.array([[R]])             # measurement noise
    kf.Q = np.array([[Q, 0], [0, Q]])  # process noise
    kf.x = np.array([[series[0]], [0]])  # initial state
    kf.P *= 100

    smoothed = np.zeros(len(series))
    velocities = np.zeros(len(series))
    for i, z in enumerate(series):
        kf.predict()
        kf.update(np.array([[z]]))
        smoothed[i] = kf.x[0, 0]
        velocities[i] = kf.x[1, 0]

    return smoothed, velocities


def kalman_direction(velocities: np.ndarray, idx: int, lookback: int = 5) -> float:
    """Kalman-based direction score from velocity.
    Returns: positive = uptrend, negative = downtrend, magnitude = confidence."""
    if idx < lookback:
        return 0.0
    recent = velocities[idx - lookback:idx + 1]
    avg_vel = np.mean(recent)
    # Normalize by recent price volatility
    return avg_vel


# ======================================================================
# Feature extraction
# ======================================================================

def extract_features(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                     volume: np.ndarray, idx: int, lookback: int = 24,
                     # Optional denoised series
                     close_dn: np.ndarray = None,
                     kalman_vel: np.ndarray = None) -> dict:
    """Extract features at idx. Optionally includes denoised features."""
    if idx < lookback + 10 or idx >= len(close):
        return {}

    f = {}
    c = close[idx]

    # Price action (raw)
    f["ret_24h"] = (c / close[idx - lookback] - 1) * 100
    f["ret_12h"] = (c / close[idx - lookback // 2] - 1) * 100
    f["ret_6h"] = (c / close[idx - lookback // 4] - 1) * 100
    f["abs_ret_24h"] = abs(f["ret_24h"])

    # Volatility
    rets = np.diff(close[idx - lookback:idx + 1]) / close[idx - lookback:idx]
    f["volatility"] = float(np.std(rets) * 100)

    f["range_pct"] = (np.max(high[idx - lookback:idx]) -
                      np.min(low[idx - lookback:idx])) / c * 100

    # Volume
    avg_vol = np.mean(volume[max(0, idx - lookback * 3):idx - lookback])
    recent_vol = np.mean(volume[idx - lookback:idx])
    f["vol_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0
    f["vol_spike"] = float(np.max(volume[idx - lookback:idx]) / avg_vol) if avg_vol > 0 else 1.0

    # RSI (baseline direction)
    if idx >= 14:
        deltas = np.diff(close[idx - 14:idx + 1])
        gains = np.mean(np.clip(deltas, 0, None))
        losses = np.mean(np.clip(-deltas, 0, None))
        rs = gains / losses if losses > 0 else 100
        f["rsi"] = 100 - (100 / (1 + rs))
    else:
        f["rsi"] = 50

    # Volatility contraction
    if idx >= 40:
        std_20 = np.std(close[idx - 20:idx])
        std_prev = np.std(close[idx - 40:idx - 20])
        f["vol_contraction"] = float(std_20 / std_prev) if std_prev > 0 else 1.0
    else:
        f["vol_contraction"] = 1.0

    # --- DENOISED FEATURES ---
    if close_dn is not None:
        cd = close_dn[idx]
        # Denoised returns (less noisy trend)
        f["dn_ret_24h"] = (cd / close_dn[idx - lookback] - 1) * 100
        f["dn_ret_12h"] = (cd / close_dn[idx - lookback // 2] - 1) * 100
        f["dn_ret_6h"] = (cd / close_dn[idx - lookback // 4] - 1) * 100

        # Noise level: divergence between raw and denoised
        f["noise_level"] = abs(c - cd) / c * 100

        # Denoised volatility
        dn_rets = np.diff(close_dn[idx - lookback:idx + 1]) / close_dn[idx - lookback:idx]
        f["dn_volatility"] = float(np.std(dn_rets) * 100)

        # SNR proxy: raw vol / denoised vol
        f["snr"] = f["volatility"] / f["dn_volatility"] if f["dn_volatility"] > 0 else 1.0

    # --- KALMAN FEATURES ---
    if kalman_vel is not None:
        vel = kalman_vel[idx]
        f["kalman_vel"] = vel
        f["kalman_vel_abs"] = abs(vel)

        # Velocity trend (accelerating?)
        if idx >= 5:
            f["kalman_accel"] = kalman_vel[idx] - kalman_vel[idx - 5]
        else:
            f["kalman_accel"] = 0

        # Velocity sign consistency (last 10 bars)
        if idx >= 10:
            signs = np.sign(kalman_vel[idx - 10:idx + 1])
            f["kalman_consistency"] = abs(np.mean(signs))
        else:
            f["kalman_consistency"] = 0

    return f


# ======================================================================
# Label + backtest
# ======================================================================

def label_big_moves(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                    min_move: float = 5.0, window: int = 6,
                    cooldown: int = 12) -> np.ndarray:
    labels = np.zeros(len(close), dtype=int)
    last_idx = -cooldown
    for i in range(window, len(close) - window):
        if i - last_idx < cooldown:
            continue
        entry = close[i]
        fh = np.max(high[i + 1:i + 1 + window])
        fl = np.min(low[i + 1:i + 1 + window])
        up = (fh - entry) / entry * 100
        down = (entry - fl) / entry * 100
        if up >= min_move or down >= min_move:
            labels[i] = 1
            last_idx = i
    return labels


def get_direction(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                  idx: int, window: int = 6) -> str:
    entry = close[idx]
    fh = np.max(high[idx + 1:idx + 1 + window])
    fl = np.min(low[idx + 1:idx + 1 + window])
    return "up" if (fh - entry) > (entry - fl) else "down"


def walk_forward(datasets: dict, mode: str = "baseline",
                 threshold: float = 0.7) -> pd.DataFrame:
    """Walk-forward backtest.
    mode: 'baseline' (raw+RSI), 'wavelet', 'kalman', 'both'
    """
    feature_cols = None
    all_trades = []

    max_len = max(len(d["close"]) for d in datasets.values())
    start = 0
    window_num = 0

    while start + TRAIN_CANDLES + TEST_CANDLES <= max_len:
        train_end = start + TRAIN_CANDLES
        test_end = train_end + TEST_CANDLES

        # Collect training data
        train_X, train_y = [], []
        for sym, d in datasets.items():
            if len(d["close"]) < train_end:
                continue

            labels = label_big_moves(d["close"][:train_end], d["high"][:train_end],
                                      d["low"][:train_end], MOVE_PCT, MOVE_WINDOW)

            pos_idx = np.where(labels == 1)[0].tolist()
            neg_all = [i for i in np.where(labels == 0)[0].tolist()
                       if i > 34 and all(abs(i - p) > 24 for p in pos_idx)]
            np.random.seed(42 + window_num)
            n_neg = min(len(pos_idx) * 2, len(neg_all))
            neg_idx = list(np.random.choice(neg_all, n_neg, replace=False)) if n_neg > 0 else []

            close_dn = d.get("close_dn") if mode in ("wavelet", "both") else None
            kalman_vel = d.get("kalman_vel") if mode in ("kalman", "both") else None

            for idx in pos_idx + neg_idx:
                feat = extract_features(
                    d["close"], d["high"], d["low"], d["volume"], idx,
                    close_dn=close_dn, kalman_vel=kalman_vel,
                )
                if feat:
                    if feature_cols is None:
                        feature_cols = sorted(feat.keys())
                    train_X.append([feat.get(c, 0) for c in feature_cols])
                    train_y.append(labels[idx])

        if len(train_X) < 50:
            start += STEP_CANDLES
            window_num += 1
            continue

        train_X = np.array(train_X)
        train_y = np.array(train_y)

        clf = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            min_samples_leaf=max(5, len(train_X) // 50),
            subsample=0.8, random_state=42,
        )
        clf.fit(train_X, train_y)

        # Test
        for sym, d in datasets.items():
            t_start = max(train_end, 50)
            t_end = min(test_end, len(d["close"]) - MOVE_WINDOW)
            if t_start >= t_end:
                continue

            close_dn = d.get("close_dn") if mode in ("wavelet", "both") else None
            kalman_vel = d.get("kalman_vel") if mode in ("kalman", "both") else None

            last_entry = -6
            active = None

            for i in range(t_start, t_end):
                # Check active trade
                if active is not None:
                    h, l = d["high"][i], d["low"][i]
                    outcome, exit_p = None, None
                    if active["long"]:
                        if l <= active["sl"]:
                            outcome, exit_p = "sl", active["sl"]
                        elif h >= active["tp"]:
                            outcome, exit_p = "tp", active["tp"]
                        elif i - active["idx"] > MAX_HOLD:
                            outcome, exit_p = "timeout", d["close"][i]
                    else:
                        if h >= active["sl"]:
                            outcome, exit_p = "sl", active["sl"]
                        elif l <= active["tp"]:
                            outcome, exit_p = "tp", active["tp"]
                        elif i - active["idx"] > MAX_HOLD:
                            outcome, exit_p = "timeout", d["close"][i]

                    if outcome:
                        ep = active["price"]
                        pnl = ((exit_p - ep) / ep * 100 if active["long"]
                               else (ep - exit_p) / ep * 100) - 2 * FEE_BPS / 100
                        all_trades.append({
                            "sym": sym, "pnl": pnl, "outcome": outcome,
                            "direction": "long" if active["long"] else "short",
                            "prob": active["prob"], "window": window_num,
                            "hold": i - active["idx"],
                        })
                        active = None
                    else:
                        continue

                if i - last_entry < 6:
                    continue

                feat = extract_features(
                    d["close"], d["high"], d["low"], d["volume"], i,
                    close_dn=close_dn, kalman_vel=kalman_vel,
                )
                if not feat:
                    continue

                x = np.array([[feat.get(c, 0) for c in feature_cols]])
                prob = clf.predict_proba(x)[0][1]
                if prob < threshold:
                    continue

                # Direction
                if mode in ("kalman", "both") and kalman_vel is not None:
                    vel = kalman_vel[i]
                    if abs(vel) < 0.5:  # too uncertain
                        continue
                    is_long = vel > 0
                else:
                    rsi = feat.get("rsi", 50)
                    if 45 <= rsi <= 55:
                        continue
                    is_long = rsi > 55

                price = d["close"][i]
                sl = price * (1 - SL_PCT / 100) if is_long else price * (1 + SL_PCT / 100)
                tp = price * (1 + SL_PCT * RR / 100) if is_long else price * (1 - SL_PCT * RR / 100)

                active = {"idx": i, "price": price, "sl": sl, "tp": tp,
                          "long": is_long, "prob": prob}
                last_entry = i

        start += STEP_CANDLES
        window_num += 1

    return pd.DataFrame(all_trades), feature_cols, clf


# ======================================================================
# Analysis
# ======================================================================

def analyze(df: pd.DataFrame, name: str) -> dict:
    if df.empty:
        print(f"  {name}: NO TRADES")
        return {}

    n = len(df)
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    wr = len(wins) / n
    pf = abs(wins["pnl"].sum() / losses["pnl"].sum()) if len(losses) > 0 and losses["pnl"].sum() != 0 else 0
    avg = df["pnl"].mean()
    total = df["pnl"].sum()

    # Equity curve drawdown
    cum = np.cumsum(df["pnl"].values)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum).max()

    print(f"  {name}:")
    print(f"    N={n}  WR={wr*100:.1f}%  PF={pf:.2f}  avg={avg:+.3f}%  "
          f"total={total:+.1f}%  maxDD={dd:.1f}%")

    # By direction
    for d in ["long", "short"]:
        sub = df[df["direction"] == d]
        if len(sub) > 0:
            sw = (sub["pnl"] > 0).mean() * 100
            print(f"    {d:>5}: {len(sub)}t WR={sw:.0f}% avg={sub['pnl'].mean():+.3f}%")

    return {"name": name, "n": n, "wr": wr, "pf": pf, "avg": avg,
            "total": total, "dd": dd}


def main():
    print("=" * 70)
    print("  BIG MOVE DETECTOR: DENOISED EDITION")
    print("=" * 70)
    print(f"  Filters: Wavelet (db4 L3 soft), Kalman (R=1 Q=0.01)")
    print(f"  Symbols: {len(SYMBOLS)}")
    print(f"  Walk-forward: train {TRAIN_CANDLES//24}d, test {TEST_CANDLES//24}d\n")

    # Load data
    datasets = {}
    for sym in SYMBOLS:
        key = sym.replace("/", "")
        path = DATA / f"{key}_1h.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        close = df["close"].values.astype(float)
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        volume = df["volume"].values.astype(float)

        # Apply filters
        close_dn = wavelet_denoise(close)
        kalman_smooth_price, kalman_vel = kalman_smooth(close)

        datasets[sym] = {
            "close": close, "high": high, "low": low, "volume": volume,
            "close_dn": close_dn, "kalman_vel": kalman_vel,
            "kalman_price": kalman_smooth_price,
        }
        print(f"  {sym}: {len(close)} candles, "
              f"noise={np.mean(np.abs(close - close_dn) / close) * 100:.3f}%",
              flush=True)

    if not datasets:
        print("  No data!")
        return

    # Run 4 configurations
    results = []
    for mode, thr, label in [
        ("baseline", 0.7, "Baseline (raw + RSI)"),
        ("wavelet",  0.7, "Wavelet denoised features"),
        ("kalman",   0.7, "Kalman direction (replaces RSI)"),
        ("both",     0.7, "Wavelet + Kalman combined"),
        ("baseline", 0.6, "Baseline thr=0.6"),
        ("both",     0.6, "Both thr=0.6"),
    ]:
        print(f"\n  Running: {label}...", flush=True)
        trades_df, feat_cols, clf = walk_forward(datasets, mode=mode, threshold=thr)
        r = analyze(trades_df, label)
        if r:
            results.append(r)

    # Comparison table
    if results:
        print(f"\n{'='*70}")
        print(f"  COMPARISON")
        print(f"{'='*70}\n")
        print(f"  {'Config':<35s} {'N':>4s} {'WR':>6s} {'PF':>6s} {'Avg':>8s} {'Total':>8s} {'DD':>6s}")
        print("  " + "-" * 75)
        for r in sorted(results, key=lambda x: x["total"], reverse=True):
            print(f"  {r['name']:<35s} {r['n']:>4d} {r['wr']*100:>5.1f}% {r['pf']:>5.2f} "
                  f"{r['avg']:>+7.3f}% {r['total']:>+7.1f}% {r['dd']:>5.1f}%")

    # Feature importance (last model)
    if feat_cols and clf:
        fi = sorted(zip(feat_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
        print(f"\n  Feature importance (last window, last mode):")
        for name, imp in fi[:15]:
            bar = "#" * int(imp * 200)
            print(f"    {name:<25s} {imp:.4f} {bar}")


if __name__ == "__main__":
    main()
