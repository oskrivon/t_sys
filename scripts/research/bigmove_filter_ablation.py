"""Big Move: filter ablation study.

Compare baseline (raw+RSI) vs wavelet vs kalman vs both on SAME data/period.
Focus: what exactly does each filter improve and worsen?

Metrics per mode:
  - WR, PF, avg PnL (overall + long/short)
  - Signal quality: precision (TP rate), false positive rate
  - Direction accuracy: % correct direction calls
  - Timing: avg hold time for wins vs losses
  - Outcome breakdown: TP/SL/timeout counts
  - Monthly consistency
  - Risk: maxDD, worst trade, avg losing trade
"""
from __future__ import annotations
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
import pywt
from filterpy.kalman import KalmanFilter as KF
from sklearn.ensemble import GradientBoostingClassifier

import builtins
_print = builtins.print
def print(*a, **kw):
    kw.setdefault("flush", True)
    _print(*a, **kw)

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
MAX_CANDLES = 24 * 240


# ======================================================================
# Filters
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
    sm = np.zeros(len(series))
    vel = np.zeros(len(series))
    for i, z in enumerate(series):
        kf.predict()
        kf.update(np.array([[z]]))
        sm[i] = kf.x[0, 0]
        vel[i] = kf.x[1, 0]
    return sm, vel


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


def get_actual_direction(close, high, low, idx, window=6):
    """What direction actually moved more in the next window."""
    if idx + 1 + window > len(close):
        return None
    e = close[idx]
    fh = np.max(high[idx+1:idx+1+window])
    fl = np.min(low[idx+1:idx+1+window])
    up = (fh - e) / e * 100
    down = (e - fl) / e * 100
    return "up" if up > down else "down"


# ======================================================================
# Walk-forward with detailed trade logging
# ======================================================================

def run_mode(datasets_raw, mode, threshold=0.7, long_only=False,
             vel_threshold=0.5, wav_level=3, kalman_R=1.0, kalman_Q=0.01):
    """Run one mode, return detailed trade list."""

    # Precompute filters
    datasets = {}
    for sym, raw in datasets_raw.items():
        close = raw["close"]
        d = dict(raw)
        if mode in ("wavelet", "both"):
            d["close_dn"] = wavelet_denoise(close, level=wav_level)
        if mode in ("kalman", "both"):
            _, d["kalman_vel"] = kalman_smooth(close, R=kalman_R, Q=kalman_Q)
        datasets[sym] = d

    feature_cols = None
    trades = []
    signals_total = 0  # how many times ML said "big move"
    signals_filtered = 0  # how many passed direction filter

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

            close_dn = d.get("close_dn")
            kvel = d.get("kalman_vel")

            for idx in pos + neg:
                feat = extract_features(d["close"], d["high"], d["low"], d["volume"],
                                        idx, close_dn=close_dn, kalman_vel=kvel)
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

            close_dn = d.get("close_dn")
            kvel = d.get("kalman_vel")
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

                        actual_dir = active.get("actual_dir")
                        predicted_dir = "up" if active["long"] else "down"
                        dir_correct = (actual_dir == predicted_dir) if actual_dir else None

                        trades.append({
                            "sym": sym, "pnl": pnl, "outcome": outcome,
                            "direction": "L" if active["long"] else "S",
                            "hold": i - active["idx"], "window": wn,
                            "prob": active["prob"],
                            "actual_dir": actual_dir,
                            "dir_correct": dir_correct,
                        })
                        active = None
                    else:
                        continue

                if i - last_entry < 6:
                    continue

                feat = extract_features(d["close"], d["high"], d["low"], d["volume"],
                                        i, close_dn=close_dn, kalman_vel=kvel)
                if not feat:
                    continue

                x = np.array([[feat.get(c, 0) for c in feature_cols]])
                prob = clf.predict_proba(x)[0][1]
                if prob < threshold:
                    continue

                signals_total += 1

                # Direction decision — THIS IS WHAT DIFFERS BETWEEN MODES
                if mode in ("kalman", "both") and kvel is not None:
                    vel = kvel[i]
                    if abs(vel) < vel_threshold:
                        continue
                    is_long = vel > 0
                else:
                    rsi = feat.get("rsi", 50)
                    if 45 <= rsi <= 55:
                        continue
                    is_long = rsi > 55

                if long_only and not is_long:
                    continue

                signals_filtered += 1
                actual_dir = get_actual_direction(d["close"], d["high"], d["low"], i)

                price = d["close"][i]
                sl = price * (1 - SL_PCT/100) if is_long else price * (1 + SL_PCT/100)
                tp = price * (1 + SL_PCT*RR/100) if is_long else price * (1 - SL_PCT*RR/100)
                active = {"idx": i, "price": price, "sl": sl, "tp": tp,
                          "long": is_long, "prob": prob, "actual_dir": actual_dir}
                last_entry = i

        start += STEP_CANDLES; wn += 1

    return trades, signals_total, signals_filtered, feature_cols, clf


# ======================================================================
# Analysis
# ======================================================================

def detailed_analysis(trades, signals_total, signals_filtered, mode_name):
    """Print comprehensive analysis of one mode."""
    print(f"\n{'='*70}")
    print(f"  MODE: {mode_name}")
    print(f"{'='*70}")

    if not trades:
        print("  NO TRADES")
        return {}

    n = len(trades)
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = (pnls > 0).mean()
    pf = abs(wins.sum() / losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 0
    avg = pnls.mean()
    total = pnls.sum()

    cum = np.cumsum(pnls)
    dd = (np.maximum.accumulate(cum) - cum).max()

    # --- 1. Overall ---
    print(f"\n  OVERALL:")
    print(f"    Trades: {n}  |  Signals: {signals_total} detected, "
          f"{signals_filtered} passed direction filter ({signals_filtered/signals_total*100:.0f}%)" if signals_total > 0 else "")
    print(f"    WR: {wr*100:.1f}%  PF: {pf:.2f}  Avg: {avg:+.3f}%  Total: {total:+.1f}%  MaxDD: {dd:.1f}%")
    print(f"    Avg win: {wins.mean():+.3f}%  Avg loss: {losses.mean():+.3f}%" if len(wins) > 0 and len(losses) > 0 else "")

    # --- 2. Direction breakdown ---
    print(f"\n  BY DIRECTION:")
    for d_label, d_code in [("Long", "L"), ("Short", "S")]:
        sub = [t for t in trades if t["direction"] == d_code]
        if not sub:
            print(f"    {d_label}: 0 trades")
            continue
        sub_pnls = np.array([t["pnl"] for t in sub])
        sub_wr = (sub_pnls > 0).mean() * 100
        sub_avg = sub_pnls.mean()
        sub_total = sub_pnls.sum()
        print(f"    {d_label}: {len(sub)}t  WR={sub_wr:.1f}%  avg={sub_avg:+.3f}%  total={sub_total:+.1f}%")

    # --- 3. Direction accuracy ---
    dir_trades = [t for t in trades if t["dir_correct"] is not None]
    if dir_trades:
        dir_correct = sum(1 for t in dir_trades if t["dir_correct"])
        dir_accuracy = dir_correct / len(dir_trades) * 100
        print(f"\n  DIRECTION ACCURACY:")
        print(f"    Correct: {dir_correct}/{len(dir_trades)} ({dir_accuracy:.1f}%)")

        # Direction accuracy vs trade outcome
        correct_wins = [t for t in dir_trades if t["dir_correct"] and t["pnl"] > 0]
        correct_losses = [t for t in dir_trades if t["dir_correct"] and t["pnl"] <= 0]
        wrong_wins = [t for t in dir_trades if not t["dir_correct"] and t["pnl"] > 0]
        wrong_losses = [t for t in dir_trades if not t["dir_correct"] and t["pnl"] <= 0]
        print(f"    Correct dir + win: {len(correct_wins)}  Correct dir + loss: {len(correct_losses)}")
        print(f"    Wrong dir + win:   {len(wrong_wins)}  Wrong dir + loss:   {len(wrong_losses)}")

    # --- 4. Outcome breakdown ---
    print(f"\n  OUTCOME BREAKDOWN:")
    for oc in ["tp", "sl", "to"]:
        sub = [t for t in trades if t["outcome"] == oc]
        if not sub:
            continue
        sub_pnls = [t["pnl"] for t in sub]
        label = {"tp": "Take Profit", "sl": "Stop Loss", "to": "Timeout"}[oc]
        avg_pnl = np.mean(sub_pnls)
        print(f"    {label:>12s}: {len(sub):>3d} ({len(sub)/n*100:>5.1f}%)  avg={avg_pnl:+.3f}%")

    # --- 5. Hold time ---
    print(f"\n  HOLD TIME (candles):")
    win_holds = [t["hold"] for t in trades if t["pnl"] > 0]
    loss_holds = [t["hold"] for t in trades if t["pnl"] <= 0]
    if win_holds:
        print(f"    Winners:  avg={np.mean(win_holds):.1f}  median={np.median(win_holds):.0f}")
    if loss_holds:
        print(f"    Losers:   avg={np.mean(loss_holds):.1f}  median={np.median(loss_holds):.0f}")

    # --- 6. Monthly ---
    windows = defaultdict(list)
    for t in trades:
        windows[t["window"]].append(t["pnl"])
    monthly_pnls = [sum(v) for v in windows.values()]
    n_pos_months = sum(1 for p in monthly_pnls if p > 0)
    print(f"\n  MONTHLY: {n_pos_months}/{len(monthly_pnls)} positive")

    # --- 7. Risk ---
    print(f"\n  RISK:")
    print(f"    Worst trade: {pnls.min():+.3f}%")
    print(f"    Best trade:  {pnls.max():+.3f}%")
    if len(losses) > 0:
        print(f"    Avg losing trade: {losses.mean():+.3f}%")
    # Consecutive losses
    max_consec_loss = 0
    curr = 0
    for p in pnls:
        if p <= 0:
            curr += 1
            max_consec_loss = max(max_consec_loss, curr)
        else:
            curr = 0
    print(f"    Max consecutive losses: {max_consec_loss}")

    # --- 8. Per-symbol ---
    print(f"\n  PER SYMBOL:")
    syms = defaultdict(list)
    for t in trades:
        syms[t["sym"]].append(t["pnl"])
    for sym in sorted(syms.keys()):
        sp = syms[sym]
        swr = np.mean([p > 0 for p in sp]) * 100
        print(f"    {sym:<12s} {len(sp):>3d}t  WR={swr:>5.1f}%  total={sum(sp):>+7.1f}%")

    return {
        "mode": mode_name, "n": n, "wr": wr, "pf": pf, "avg": avg,
        "total": total, "dd": dd,
        "dir_accuracy": dir_accuracy if dir_trades else None,
        "signals_total": signals_total, "signals_filtered": signals_filtered,
        "n_pos_months": n_pos_months, "n_months": len(monthly_pnls),
        "max_consec_loss": max_consec_loss,
    }


def main():
    print("=" * 70)
    print("  BIG MOVE FILTER ABLATION STUDY")
    print("=" * 70)
    print("  Question: what does each filter improve/worsen vs baseline?")
    print()

    # Load data
    datasets_raw = {}
    for sym in SYMBOLS:
        key = sym.replace("/", "")
        path = DATA / f"{key}_1h.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if len(df) > MAX_CANDLES:
            df = df.iloc[-MAX_CANDLES:].reset_index(drop=True)
        datasets_raw[sym] = {
            "close": df["close"].values.astype(float),
            "high": df["high"].values.astype(float),
            "low": df["low"].values.astype(float),
            "volume": df["volume"].values.astype(float),
        }
        print(f"  {sym}: {len(df)} candles")

    if not datasets_raw:
        print("  No data!"); return

    # ============================
    # Run 6 configurations
    # ============================

    configs = [
        # (name, mode, threshold, long_only, vel_thr, wav_level, R, Q)
        ("1. Baseline (RSI, L+S)",         "baseline", 0.7, False, 0.5, 3, 1.0, 0.01),
        ("2. Wavelet features (RSI, L+S)",  "wavelet",  0.7, False, 0.5, 3, 1.0, 0.01),
        ("3. Kalman direction (L+S)",       "kalman",   0.7, False, 0.5, 3, 1.0, 0.01),
        ("4. Both (L+S)",                   "both",     0.7, False, 0.5, 3, 1.0, 0.01),
        ("5. Baseline (RSI, long-only)",    "baseline", 0.7, True,  0.5, 3, 1.0, 0.01),
        ("6. Kalman (long-only)",           "kalman",   0.7, True,  0.5, 3, 1.0, 0.01),
        ("7. Both (long-only)",             "both",     0.7, True,  0.5, 3, 1.0, 0.01),
        ("8. Both (LO, tuned best)",        "both",     0.8, True,  0.3, 4, 1.0, 0.01),
    ]

    results = []
    for name, mode, thr, lo, vt, wl, R, Q in configs:
        print(f"\n  Running: {name}...", end="")
        trades, sig_total, sig_filtered, feat_cols, clf = run_mode(
            datasets_raw, mode, threshold=thr, long_only=lo,
            vel_threshold=vt, wav_level=wl, kalman_R=R, kalman_Q=Q,
        )
        r = detailed_analysis(trades, sig_total, sig_filtered, name)
        results.append(r)

        # Feature importance for modes with denoised features
        if feat_cols and clf and mode in ("wavelet", "both") and name == configs[-1][0]:
            fi = sorted(zip(feat_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
            print(f"\n  FEATURE IMPORTANCE (last window, {name}):")
            for fname, imp in fi[:12]:
                bar = "#" * int(imp * 150)
                # Mark denoised features
                tag = " [DENOISED]" if fname.startswith("dn_") or fname.startswith("kalman_") or fname in ("noise_level", "snr") else ""
                print(f"    {fname:<25s} {imp:.4f} {bar}{tag}")

    # ============================
    # Comparison table
    # ============================
    valid = [r for r in results if r.get("n", 0) > 0]

    print(f"\n\n{'='*70}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*70}\n")

    print(f"  {'Mode':<35s} {'N':>4s} {'WR':>6s} {'PF':>5s} {'Avg':>8s} {'Total':>8s} "
          f"{'DD':>6s} {'DirAcc':>7s} {'Months+':>8s} {'ConsL':>6s}")
    print("  " + "-" * 100)

    for r in valid:
        da = f"{r['dir_accuracy']:.1f}%" if r.get('dir_accuracy') is not None else "N/A"
        months = f"{r['n_pos_months']}/{r['n_months']}" if r.get('n_months') else "N/A"
        print(f"  {r['mode']:<35s} {r['n']:>4d} {r['wr']*100:>5.1f}% {r['pf']:>4.2f} "
              f"{r['avg']:>+7.3f}% {r['total']:>+7.1f}% {r['dd']:>5.1f}% "
              f"{da:>7s} {months:>8s} {r.get('max_consec_loss', 0):>6d}")

    # ============================
    # Delta analysis: what changed?
    # ============================
    print(f"\n\n{'='*70}")
    print(f"  DELTA ANALYSIS (vs Baseline L+S)")
    print(f"{'='*70}\n")

    baseline = next((r for r in valid if "Baseline" in r["mode"] and "long" not in r["mode"]), None)
    if baseline:
        for r in valid:
            if r is baseline:
                continue
            dwr = (r["wr"] - baseline["wr"]) * 100
            dpf = r["pf"] - baseline["pf"]
            davg = r["avg"] - baseline["avg"]
            dtotal = r["total"] - baseline["total"]
            ddd = r["dd"] - baseline["dd"]
            dn = r["n"] - baseline["n"]

            print(f"  {r['mode']}:")
            print(f"    ΔN={dn:+d}  ΔWR={dwr:+.1f}pp  ΔPF={dpf:+.2f}  "
                  f"ΔAvg={davg:+.3f}%  ΔTotal={dtotal:+.1f}%  ΔDD={ddd:+.1f}%")

            # Interpret
            improvements = []
            worsened = []
            if dwr > 2: improvements.append(f"WR +{dwr:.0f}pp")
            elif dwr < -2: worsened.append(f"WR {dwr:.0f}pp")
            if dpf > 0.1: improvements.append(f"PF +{dpf:.2f}")
            elif dpf < -0.1: worsened.append(f"PF {dpf:.2f}")
            if davg > 0.05: improvements.append(f"avg +{davg:.3f}%")
            elif davg < -0.05: worsened.append(f"avg {davg:.3f}%")
            if ddd < -3: improvements.append(f"DD {ddd:.0f}%")
            elif ddd > 3: worsened.append(f"DD +{ddd:.0f}%")
            if dn > 5: improvements.append(f"more signals (+{dn})")
            elif dn < -5: worsened.append(f"fewer signals ({dn})")

            if improvements:
                print(f"    ✓ Improved: {', '.join(improvements)}")
            if worsened:
                print(f"    ✗ Worsened: {', '.join(worsened)}")
            if not improvements and not worsened:
                print(f"    ~ No significant change")
            print()


if __name__ == "__main__":
    main()
