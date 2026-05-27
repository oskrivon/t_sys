"""Big Move Denoised: parameter grid search.

Tune: Kalman R/Q, wavelet level, long-only, threshold.
Based on bigmove_denoised.py results where Wavelet+Kalman got PF=0.98.

Usage:
    python scripts/research/bigmove_denoised_tune.py
"""
from __future__ import annotations

from pathlib import Path
from itertools import product

import numpy as np
import pandas as pd
import pywt
from filterpy.kalman import KalmanFilter as KF
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
# Filters (parameterized)
# ======================================================================

def wavelet_denoise(series, wavelet="db4", level=3):
    coeffs = pywt.wavedec(series, wavelet, level=level)
    sigma = np.median(np.abs(coeffs[-1])) / 0.6745
    thr = sigma * np.sqrt(2 * np.log(len(series)))
    dn = [coeffs[0]] + [pywt.threshold(c, thr, mode="soft") for c in coeffs[1:]]
    return pywt.waverec(dn, wavelet)[:len(series)]


def kalman_smooth(series, R=1.0, Q=0.01):
    kf = KF(dim_x=2, dim_z=1)
    kf.F = np.array([[1, 1], [0, 1]])
    kf.H = np.array([[1, 0]])
    kf.R = np.array([[R]])
    kf.Q = np.array([[Q, 0], [0, Q]])
    kf.x = np.array([[series[0]], [0]])
    kf.P *= 100
    smoothed = np.zeros(len(series))
    vel = np.zeros(len(series))
    for i, z in enumerate(series):
        kf.predict()
        kf.update(np.array([[z]]))
        smoothed[i] = kf.x[0, 0]
        vel[i] = kf.x[1, 0]
    return smoothed, vel


# ======================================================================
# Features
# ======================================================================

def extract_features(close, high, low, volume, idx, lookback=24,
                     close_dn=None, kalman_vel=None):
    if idx < lookback + 10 or idx >= len(close):
        return {}
    f = {}
    c = close[idx]

    f["ret_24h"] = (c / close[idx - lookback] - 1) * 100
    f["ret_12h"] = (c / close[idx - lookback // 2] - 1) * 100
    f["ret_6h"] = (c / close[idx - lookback // 4] - 1) * 100
    f["abs_ret_24h"] = abs(f["ret_24h"])

    rets = np.diff(close[idx - lookback:idx + 1]) / close[idx - lookback:idx]
    f["volatility"] = float(np.std(rets) * 100)
    f["range_pct"] = (np.max(high[idx-lookback:idx]) - np.min(low[idx-lookback:idx])) / c * 100

    avg_vol = np.mean(volume[max(0, idx-lookback*3):idx-lookback])
    recent_vol = np.mean(volume[idx-lookback:idx])
    f["vol_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0
    f["vol_spike"] = float(np.max(volume[idx-lookback:idx]) / avg_vol) if avg_vol > 0 else 1.0

    if idx >= 14:
        deltas = np.diff(close[idx-14:idx+1])
        g = np.mean(np.clip(deltas, 0, None))
        l = np.mean(np.clip(-deltas, 0, None))
        rs = g / l if l > 0 else 100
        f["rsi"] = 100 - (100 / (1 + rs))
    else:
        f["rsi"] = 50

    if idx >= 40:
        s20 = np.std(close[idx-20:idx])
        sp = np.std(close[idx-40:idx-20])
        f["vol_contraction"] = float(s20 / sp) if sp > 0 else 1.0
    else:
        f["vol_contraction"] = 1.0

    if close_dn is not None:
        cd = close_dn[idx]
        f["dn_ret_24h"] = (cd / close_dn[idx-lookback] - 1) * 100
        f["dn_ret_12h"] = (cd / close_dn[idx-lookback//2] - 1) * 100
        f["dn_ret_6h"] = (cd / close_dn[idx-lookback//4] - 1) * 100
        f["noise_level"] = abs(c - cd) / c * 100
        dn_rets = np.diff(close_dn[idx-lookback:idx+1]) / close_dn[idx-lookback:idx]
        f["dn_volatility"] = float(np.std(dn_rets) * 100)
        f["snr"] = f["volatility"] / f["dn_volatility"] if f["dn_volatility"] > 0 else 1.0

    if kalman_vel is not None:
        f["kalman_vel"] = kalman_vel[idx]
        f["kalman_vel_abs"] = abs(kalman_vel[idx])
        if idx >= 5:
            f["kalman_accel"] = kalman_vel[idx] - kalman_vel[idx-5]
        else:
            f["kalman_accel"] = 0
        if idx >= 10:
            signs = np.sign(kalman_vel[idx-10:idx+1])
            f["kalman_consistency"] = abs(np.mean(signs))
        else:
            f["kalman_consistency"] = 0

    return f


# ======================================================================
# Backtest
# ======================================================================

def label_big_moves(close, high, low, min_move=5.0, window=6, cooldown=12):
    labels = np.zeros(len(close), dtype=int)
    last = -cooldown
    for i in range(window, len(close) - window):
        if i - last < cooldown:
            continue
        e = close[i]
        fh = np.max(high[i+1:i+1+window])
        fl = np.min(low[i+1:i+1+window])
        if (fh-e)/e*100 >= min_move or (e-fl)/e*100 >= min_move:
            labels[i] = 1
            last = i
    return labels


def run_config(datasets_raw, kalman_R, kalman_Q, wav_level, threshold,
               long_only, vel_threshold) -> dict:
    """Run one config. Precompute filters per dataset."""
    datasets = {}
    for sym, raw in datasets_raw.items():
        close = raw["close"]
        close_dn = wavelet_denoise(close, level=wav_level)
        _, kvel = kalman_smooth(close, R=kalman_R, Q=kalman_Q)
        datasets[sym] = {**raw, "close_dn": close_dn, "kalman_vel": kvel}

    feature_cols = None
    all_trades = []
    max_len = max(len(d["close"]) for d in datasets.values())
    start = 0
    wn = 0

    while start + TRAIN_CANDLES + TEST_CANDLES <= max_len:
        train_end = start + TRAIN_CANDLES
        test_end = train_end + TEST_CANDLES

        train_X, train_y = [], []
        for sym, d in datasets.items():
            if len(d["close"]) < train_end:
                continue
            labels = label_big_moves(d["close"][:train_end], d["high"][:train_end],
                                      d["low"][:train_end])
            pos = np.where(labels == 1)[0].tolist()
            neg_all = [i for i in np.where(labels == 0)[0].tolist()
                       if i > 34 and all(abs(i-p) > 24 for p in pos)]
            np.random.seed(42 + wn)
            n_neg = min(len(pos)*2, len(neg_all))
            neg = list(np.random.choice(neg_all, n_neg, replace=False)) if n_neg > 0 else []

            for idx in pos + neg:
                feat = extract_features(d["close"], d["high"], d["low"], d["volume"],
                                        idx, close_dn=d["close_dn"], kalman_vel=d["kalman_vel"])
                if feat:
                    if feature_cols is None:
                        feature_cols = sorted(feat.keys())
                    train_X.append([feat.get(c, 0) for c in feature_cols])
                    train_y.append(labels[idx])

        if len(train_X) < 50:
            start += STEP_CANDLES; wn += 1; continue

        clf = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            min_samples_leaf=max(5, len(train_X)//50), subsample=0.8, random_state=42,
        )
        clf.fit(np.array(train_X), np.array(train_y))

        for sym, d in datasets.items():
            t_start = max(train_end, 50)
            t_end = min(test_end, len(d["close"]) - MOVE_WINDOW)
            if t_start >= t_end:
                continue

            last_entry = -6
            active = None

            for i in range(t_start, t_end):
                if active is not None:
                    h, l = d["high"][i], d["low"][i]
                    outcome, exit_p = None, None
                    if active["long"]:
                        if l <= active["sl"]: outcome, exit_p = "sl", active["sl"]
                        elif h >= active["tp"]: outcome, exit_p = "tp", active["tp"]
                        elif i - active["idx"] > MAX_HOLD: outcome, exit_p = "to", d["close"][i]
                    else:
                        if h >= active["sl"]: outcome, exit_p = "sl", active["sl"]
                        elif l <= active["tp"]: outcome, exit_p = "tp", active["tp"]
                        elif i - active["idx"] > MAX_HOLD: outcome, exit_p = "to", d["close"][i]

                    if outcome:
                        ep = active["price"]
                        pnl = ((exit_p-ep)/ep*100 if active["long"]
                               else (ep-exit_p)/ep*100) - 2*FEE_BPS/100
                        all_trades.append({"pnl": pnl, "dir": "L" if active["long"] else "S"})
                        active = None
                    else:
                        continue

                if i - last_entry < 6:
                    continue

                feat = extract_features(d["close"], d["high"], d["low"], d["volume"],
                                        i, close_dn=d["close_dn"], kalman_vel=d["kalman_vel"])
                if not feat:
                    continue

                x = np.array([[feat.get(c, 0) for c in feature_cols]])
                prob = clf.predict_proba(x)[0][1]
                if prob < threshold:
                    continue

                vel = d["kalman_vel"][i]
                if abs(vel) < vel_threshold:
                    continue
                is_long = vel > 0

                if long_only and not is_long:
                    continue

                price = d["close"][i]
                sl = price * (1 - SL_PCT/100) if is_long else price * (1 + SL_PCT/100)
                tp = price * (1 + SL_PCT*RR/100) if is_long else price * (1 - SL_PCT*RR/100)
                active = {"idx": i, "price": price, "sl": sl, "tp": tp,
                          "long": is_long, "prob": prob}
                last_entry = i

        start += STEP_CANDLES; wn += 1

    if not all_trades:
        return {"n": 0}

    pnls = np.array([t["pnl"] for t in all_trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = (pnls > 0).mean()
    pf = abs(wins.sum() / losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 0
    cum = np.cumsum(pnls)
    dd = (np.maximum.accumulate(cum) - cum).max()

    n_long = sum(1 for t in all_trades if t["dir"] == "L")
    long_pnls = [t["pnl"] for t in all_trades if t["dir"] == "L"]
    long_wr = np.mean([p > 0 for p in long_pnls]) if long_pnls else 0
    long_avg = np.mean(long_pnls) if long_pnls else 0

    return {
        "n": len(pnls), "wr": wr, "pf": pf, "avg": pnls.mean(),
        "total": pnls.sum(), "dd": dd,
        "n_long": n_long, "long_wr": long_wr, "long_avg": long_avg,
    }


def main():
    print("=" * 70)
    print("  BIG MOVE DENOISED: PARAMETER GRID SEARCH")
    print("=" * 70, flush=True)

    # Load raw data once
    datasets_raw = {}
    for sym in SYMBOLS:
        key = sym.replace("/", "")
        path = DATA / f"{key}_1h.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        datasets_raw[sym] = {
            "close": df["close"].values.astype(float),
            "high": df["high"].values.astype(float),
            "low": df["low"].values.astype(float),
            "volume": df["volume"].values.astype(float),
        }
    print(f"  Loaded {len(datasets_raw)} symbols\n", flush=True)

    # Grid
    grid = {
        "kalman_R":  [0.5, 1.0, 5.0],
        "kalman_Q":  [0.001, 0.01, 0.1],
        "wav_level": [2, 3, 4],
        "threshold": [0.6, 0.7, 0.8],
        "long_only": [False, True],
        "vel_thr":   [0.3, 0.5, 1.0],
    }

    # Full grid is 3*3*3*3*2*3 = 486 configs — too many.
    # Smart grid: fix some, vary others

    configs = []

    # Phase 1: Kalman R/Q sweep (fix others at best known)
    for R, Q in product(grid["kalman_R"], grid["kalman_Q"]):
        configs.append(("kalman_RQ", R, Q, 3, 0.7, False, 0.5))

    # Phase 2: Wavelet level (fix Kalman at results from phase 1 placeholder)
    for wl in grid["wav_level"]:
        configs.append(("wav_level", 1.0, 0.01, wl, 0.7, False, 0.5))

    # Phase 3: Threshold sweep
    for thr in grid["threshold"]:
        configs.append(("threshold", 1.0, 0.01, 3, thr, False, 0.5))

    # Phase 4: Long-only
    for lo in [True, False]:
        configs.append(("long_only", 1.0, 0.01, 3, 0.7, lo, 0.5))

    # Phase 5: Velocity threshold
    for vt in grid["vel_thr"]:
        configs.append(("vel_thr", 1.0, 0.01, 3, 0.7, False, vt))
        configs.append(("vel_thr_LO", 1.0, 0.01, 3, 0.7, True, vt))

    # Phase 6: Best combos (educated guesses)
    combos = [
        (1.0, 0.01, 3, 0.7, True, 0.5),   # baseline best
        (1.0, 0.01, 3, 0.7, True, 0.3),   # looser vel
        (1.0, 0.01, 3, 0.8, True, 0.5),   # stricter ML
        (0.5, 0.01, 3, 0.7, True, 0.5),   # less smooth kalman
        (5.0, 0.01, 3, 0.7, True, 0.5),   # more smooth kalman
        (1.0, 0.001, 3, 0.7, True, 0.5),  # slower kalman
        (1.0, 0.001, 4, 0.7, True, 0.3),  # more denoise + slow kalman
        (1.0, 0.01, 4, 0.8, True, 0.3),   # max denoise + strict ML
        (5.0, 0.001, 4, 0.7, True, 0.5),  # smoothest combo
        (0.5, 0.1, 2, 0.6, True, 0.3),    # reactive kalman + light denoise
        (1.0, 0.01, 3, 0.7, True, 1.0),   # only strong velocity
        (1.0, 0.01, 3, 0.6, True, 1.0),   # strong vel + loose ML
    ]
    for R, Q, wl, thr, lo, vt in combos:
        configs.append(("combo", R, Q, wl, thr, lo, vt))

    # Dedupe
    seen = set()
    unique_configs = []
    for c in configs:
        key = (c[1], c[2], c[3], c[4], c[5], c[6])
        if key not in seen:
            seen.add(key)
            unique_configs.append(c)

    print(f"  Total configs: {len(unique_configs)}\n", flush=True)

    # Run
    results = []
    for i, (phase, R, Q, wl, thr, lo, vt) in enumerate(unique_configs):
        label = f"R={R} Q={Q} wl={wl} thr={thr} {'LO' if lo else 'LS'} vel={vt}"
        r = run_config(datasets_raw, R, Q, wl, thr, lo, vt)
        r["label"] = label
        r["phase"] = phase
        r["R"] = R; r["Q"] = Q; r["wl"] = wl; r["thr"] = thr
        r["lo"] = lo; r["vt"] = vt
        results.append(r)

        if r["n"] > 0:
            print(f"  [{i+1}/{len(unique_configs)}] {label}: "
                  f"N={r['n']:>4d} WR={r['wr']*100:.1f}% PF={r['pf']:.2f} "
                  f"avg={r['avg']:+.3f}% total={r['total']:+.1f}%", flush=True)
        else:
            print(f"  [{i+1}/{len(unique_configs)}] {label}: NO TRADES", flush=True)

    # Sort by avg PnL
    valid = [r for r in results if r["n"] >= 20]

    print(f"\n\n{'='*70}")
    print(f"  TOP 15 CONFIGS (by avg PnL, N>=20)")
    print(f"{'='*70}\n")
    print(f"  {'Config':<45s} {'N':>4s} {'WR':>6s} {'PF':>5s} {'Avg':>8s} {'Total':>8s} {'DD':>6s}")
    print("  " + "-" * 80)

    for r in sorted(valid, key=lambda x: x["avg"], reverse=True)[:15]:
        print(f"  {r['label']:<45s} {r['n']:>4d} {r['wr']*100:>5.1f}% {r['pf']:>4.2f} "
              f"{r['avg']:>+7.3f}% {r['total']:>+7.1f}% {r['dd']:>5.1f}%")

    # Best profitable (PF > 1)
    profitable = [r for r in valid if r["pf"] > 1.0]
    if profitable:
        print(f"\n\n  PROFITABLE CONFIGS (PF > 1.0):\n")
        print(f"  {'Config':<45s} {'N':>4s} {'WR':>6s} {'PF':>5s} {'Avg':>8s} {'Total':>8s}")
        print("  " + "-" * 75)
        for r in sorted(profitable, key=lambda x: x["total"], reverse=True):
            print(f"  {r['label']:<45s} {r['n']:>4d} {r['wr']*100:>5.1f}% {r['pf']:>4.2f} "
                  f"{r['avg']:>+7.3f}% {r['total']:>+7.1f}%")

    # Long-only analysis
    print(f"\n\n  LONG-ONLY ANALYSIS (top 10):\n")
    lo_valid = [r for r in valid if r["lo"] and r["n_long"] > 0]
    print(f"  {'Config':<45s} {'NL':>4s} {'L_WR':>6s} {'L_Avg':>8s}")
    print("  " + "-" * 65)
    for r in sorted(lo_valid, key=lambda x: x["long_avg"], reverse=True)[:10]:
        print(f"  {r['label']:<45s} {r['n_long']:>4d} {r['long_wr']*100:>5.1f}% "
              f"{r['long_avg']:>+7.3f}%")

    # Parameter sensitivity
    print(f"\n\n  PARAMETER SENSITIVITY:\n")

    # Kalman R
    for param_name, param_key, phase_filter in [
        ("Kalman R", "R", "kalman_RQ"),
        ("Kalman Q", "Q", "kalman_RQ"),
        ("Wavelet level", "wl", "wav_level"),
        ("Threshold", "thr", "threshold"),
        ("Vel threshold", "vt", "vel_thr"),
    ]:
        group = [r for r in results if r["phase"] == phase_filter and r["n"] > 0]
        if not group:
            continue
        vals = sorted(set(r[param_key] for r in group))
        print(f"  {param_name}:")
        for v in vals:
            sub = [r for r in group if r[param_key] == v]
            if sub:
                avg_pf = np.mean([r["pf"] for r in sub])
                avg_wr = np.mean([r["wr"] for r in sub])
                avg_pnl = np.mean([r["avg"] for r in sub])
                print(f"    {v:>6}: avg_PF={avg_pf:.2f} avg_WR={avg_wr*100:.1f}% avg_PnL={avg_pnl:+.3f}%")
        print()


if __name__ == "__main__":
    main()
