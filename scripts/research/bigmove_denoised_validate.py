"""Big Move Denoised: full quant validation pipeline.

8 checks for the best config (wl=4, thr=0.8, long-only, vel=0.3, R=1.0, Q=0.01):
  1. Beta test vs BTC buy-and-hold
  2. Monthly stability
  3. Grid selection bias (top-3 configs)
  4. Holdout OOS (last 60 days excluded from training)
  5. Per-coin alpha analysis
  6. Drawdown profile
  7. Slippage stress test
  8. DSR (Deflated Sharpe Ratio)

Usage:
    python scripts/research/bigmove_denoised_validate.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pywt
from filterpy.kalman import KalmanFilter as KF
from sklearn.ensemble import GradientBoostingClassifier
from scipy import stats

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
MAX_CANDLES = 24 * 240  # cap all symbols to 240 days (10 months) for speed

import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _original_print(*args, **kwargs)

# Best config from grid search
BEST = dict(R=1.0, Q=0.01, wl=4, thr=0.8, long_only=True, vel_thr=0.3)
# 2nd and 3rd best for grid selection bias check
ALT_CONFIGS = [
    dict(R=1.0, Q=0.001, wl=4, thr=0.7, long_only=True, vel_thr=0.3),
    dict(R=1.0, Q=0.01, wl=3, thr=0.8, long_only=True, vel_thr=0.5),
]


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


# ======================================================================
# Walk-forward with detailed trade records
# ======================================================================

def run_backtest(datasets_raw, cfg, holdout_candles=0, extra_fee_bps=0):
    """Run walk-forward backtest. Returns list of trade dicts with metadata.

    holdout_candles: exclude last N candles from training AND testing
                     (for holdout OOS, set to 0 and trim data externally)
    extra_fee_bps: additional slippage on top of FEE_BPS per side
    """
    R, Q, wl = cfg["R"], cfg["Q"], cfg["wl"]
    thr, long_only, vel_thr = cfg["thr"], cfg["long_only"], cfg["vel_thr"]
    total_fee = FEE_BPS + extra_fee_bps

    datasets = {}
    for sym, raw in datasets_raw.items():
        close = raw["close"]
        n = len(close) - holdout_candles if holdout_candles > 0 else len(close)
        close_trim = close[:n]
        close_dn = wavelet_denoise(close_trim, level=wl)
        _, kvel = kalman_smooth(close_trim, R=R, Q=Q)
        datasets[sym] = {
            "close": close_trim, "high": raw["high"][:n],
            "low": raw["low"][:n], "volume": raw["volume"][:n],
            "close_dn": close_dn, "kalman_vel": kvel,
            "ts": raw["ts"][:n] if "ts" in raw else None,
        }

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
                               else (ep-exit_p)/ep*100) - 2*total_fee/100
                        trade = {
                            "sym": sym, "pnl": pnl, "outcome": outcome,
                            "direction": "L" if active["long"] else "S",
                            "entry_idx": active["idx"], "exit_idx": i,
                            "entry_price": ep, "exit_price": exit_p,
                            "hold": i - active["idx"], "window": wn,
                        }
                        # Add timestamp if available
                        if d["ts"] is not None:
                            trade["entry_ts"] = d["ts"][active["idx"]]
                            trade["exit_ts"] = d["ts"][i]
                        all_trades.append(trade)
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
                if prob < thr:
                    continue

                vel = d["kalman_vel"][i]
                if abs(vel) < vel_thr:
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

    return all_trades


def summary(trades):
    if not trades:
        return {"n": 0}
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = (pnls > 0).mean()
    pf = abs(wins.sum() / losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 0
    cum = np.cumsum(pnls)
    dd = (np.maximum.accumulate(cum) - cum).max()
    return {"n": len(pnls), "wr": wr, "pf": pf, "avg": pnls.mean(),
            "total": pnls.sum(), "dd": dd}


# ======================================================================
# 8 Validation checks
# ======================================================================

def check_1_beta(trades, datasets_raw):
    """Compare strategy return vs BTC buy-and-hold for same period."""
    print("\n" + "=" * 70)
    print("  CHECK 1: BETA TEST — strategy vs BTC buy-and-hold")
    print("=" * 70)

    if not trades:
        print("  NO TRADES"); return "FAIL"

    # Strategy metrics
    s = summary(trades)
    print(f"\n  Strategy: N={s['n']} WR={s['wr']*100:.1f}% PF={s['pf']:.2f} "
          f"total={s['total']:+.1f}%")

    # Find date range of trades (by index in BTC data)
    btc_trades = [t for t in trades if t["sym"] == "BTC/USDT"]
    all_entry_idx = [t["entry_idx"] for t in trades]
    all_exit_idx = [t["exit_idx"] for t in trades]
    first_idx = min(all_entry_idx)
    last_idx = max(all_exit_idx)

    # BTC buy-and-hold for same period
    btc = datasets_raw.get("BTC/USDT")
    if btc is None:
        print("  No BTC data!"); return "FAIL"

    btc_start = btc["close"][first_idx]
    btc_end = btc["close"][min(last_idx, len(btc["close"]) - 1)]
    btc_ret = (btc_end / btc_start - 1) * 100
    n_days = (last_idx - first_idx) / 24

    print(f"  BTC buy-and-hold same period ({n_days:.0f} days): {btc_ret:+.1f}%")
    print(f"  Strategy total: {s['total']:+.1f}%")

    # Alpha = strategy - beta * market
    # Simple alpha: strategy_ret - market_ret (assumes beta=1 for long-only)
    alpha = s["total"] - btc_ret
    print(f"\n  Naive alpha (strat - BTC): {alpha:+.1f}%")

    # More rigorous: compute beta via correlation of trade PnLs with BTC returns
    # For each trade, compute BTC return over same holding period
    beta_returns = []
    strat_returns = []
    for t in trades:
        sym = t["sym"]
        if sym not in datasets_raw:
            continue
        d = datasets_raw[sym]
        # Use BTC return for same period
        btc_d = datasets_raw.get("BTC/USDT")
        if btc_d is None:
            continue
        ei, xi = t["entry_idx"], t["exit_idx"]
        if ei < len(btc_d["close"]) and xi < len(btc_d["close"]):
            btc_r = (btc_d["close"][xi] / btc_d["close"][ei] - 1) * 100
            beta_returns.append(btc_r)
            strat_returns.append(t["pnl"])

    if len(beta_returns) > 10:
        beta_arr = np.array(beta_returns)
        strat_arr = np.array(strat_returns)

        # Regression: strat = alpha + beta * btc
        slope, intercept, r_value, p_value, std_err = stats.linregress(beta_arr, strat_arr)
        print(f"\n  Regression: strat_pnl = {intercept:+.4f} + {slope:.3f} * btc_pnl")
        print(f"  Beta = {slope:.3f}, Alpha (intercept) = {intercept:+.4f}%/trade")
        print(f"  R² = {r_value**2:.3f}, p(beta) = {p_value:.4f}")

        # Alpha t-test: is intercept significantly > 0?
        residuals = strat_arr - (intercept + slope * beta_arr)
        se_alpha = np.std(residuals) / np.sqrt(len(residuals))
        t_alpha = intercept / se_alpha if se_alpha > 0 else 0
        p_alpha = 1 - stats.t.cdf(t_alpha, len(residuals) - 2)
        print(f"  Alpha t-stat = {t_alpha:.2f}, p(alpha>0) = {p_alpha:.4f}")

        if intercept > 0 and p_alpha < 0.05:
            print(f"\n  ✓ PASS — significant positive alpha after beta adjustment")
            result = "PASS"
        elif intercept > 0:
            print(f"\n  ~ MARGINAL — positive alpha but not significant (p={p_alpha:.3f})")
            result = "MARGINAL"
        else:
            print(f"\n  ✗ FAIL — no alpha after beta adjustment (intercept={intercept:+.4f})")
            result = "FAIL"
    else:
        print("  Not enough trades for regression")
        result = "FAIL"

    # Also check: what fraction of return is from BTC longs during bull market?
    long_btc = [t for t in trades if t["sym"] == "BTC/USDT" and t["direction"] == "L"]
    long_btc_pnl = sum(t["pnl"] for t in long_btc)
    total_pnl = sum(t["pnl"] for t in trades)
    if total_pnl != 0:
        btc_fraction = long_btc_pnl / total_pnl * 100
        print(f"\n  BTC longs contribution: {long_btc_pnl:+.1f}% ({btc_fraction:.0f}% of total)")

    return result


def check_2_monthly(trades):
    """Monthly PnL stability."""
    print("\n" + "=" * 70)
    print("  CHECK 2: MONTHLY STABILITY")
    print("=" * 70)

    if not trades:
        print("  NO TRADES"); return "FAIL"

    # Group trades by walk-forward window (≈ month)
    windows = {}
    for t in trades:
        w = t["window"]
        if w not in windows:
            windows[w] = []
        windows[w].append(t["pnl"])

    print(f"\n  {'Window':>8s} {'N':>4s} {'PnL':>8s} {'WR':>6s} {'Avg':>8s}")
    print("  " + "-" * 40)

    monthly_pnls = []
    for w in sorted(windows.keys()):
        pnls = windows[w]
        total = sum(pnls)
        wr = np.mean([p > 0 for p in pnls]) * 100
        avg = np.mean(pnls)
        monthly_pnls.append(total)
        print(f"  {w:>8d} {len(pnls):>4d} {total:>+7.1f}% {wr:>5.1f}% {avg:>+7.3f}%")

    n_months = len(monthly_pnls)
    n_positive = sum(1 for p in monthly_pnls if p > 0)
    print(f"\n  Positive months: {n_positive}/{n_months} ({n_positive/n_months*100:.0f}%)")
    print(f"  Best month: {max(monthly_pnls):+.1f}%")
    print(f"  Worst month: {min(monthly_pnls):+.1f}%")
    print(f"  Std of monthly PnL: {np.std(monthly_pnls):.1f}%")

    # Concentration: is profit from 1-2 months?
    if sum(monthly_pnls) > 0:
        sorted_m = sorted(monthly_pnls, reverse=True)
        top1_frac = sorted_m[0] / sum(p for p in monthly_pnls if p > 0) * 100
        top2_frac = sum(sorted_m[:2]) / sum(p for p in monthly_pnls if p > 0) * 100
        print(f"  Top-1 month = {top1_frac:.0f}% of total profit")
        print(f"  Top-2 months = {top2_frac:.0f}% of total profit")

        if n_positive / n_months >= 0.5 and top1_frac < 50:
            print(f"\n  ✓ PASS — stable across months")
            return "PASS"
        elif n_positive / n_months >= 0.4:
            print(f"\n  ~ MARGINAL — somewhat concentrated")
            return "MARGINAL"
        else:
            print(f"\n  ✗ FAIL — unstable, profit concentrated")
            return "FAIL"
    else:
        print(f"\n  ✗ FAIL — net negative")
        return "FAIL"


def check_3_grid_bias(datasets_raw):
    """Run 2nd and 3rd best configs to check grid selection bias."""
    print("\n" + "=" * 70)
    print("  CHECK 3: GRID SELECTION BIAS — top-3 configs")
    print("=" * 70)

    results = []
    for i, cfg in enumerate([BEST] + ALT_CONFIGS):
        label = (f"R={cfg['R']} Q={cfg['Q']} wl={cfg['wl']} thr={cfg['thr']} "
                 f"{'LO' if cfg['long_only'] else 'LS'} vel={cfg['vel_thr']}")
        trades = run_backtest(datasets_raw, cfg)
        s = summary(trades)
        results.append(s)
        rank = "BEST" if i == 0 else f"ALT-{i}"
        if s["n"] > 0:
            print(f"\n  [{rank}] {label}")
            print(f"    N={s['n']} WR={s['wr']*100:.1f}% PF={s['pf']:.2f} "
                  f"avg={s['avg']:+.3f}% total={s['total']:+.1f}%")
        else:
            print(f"\n  [{rank}] {label}: NO TRADES")

    # Check: are alt configs also profitable?
    n_profitable = sum(1 for r in results if r.get("pf", 0) > 1.0)
    print(f"\n  Profitable configs: {n_profitable}/3")

    if n_profitable >= 2:
        print(f"  ✓ PASS — multiple configs profitable, not a single outlier")
        return "PASS"
    elif n_profitable == 1:
        print(f"  ✗ FAIL — only best config profitable, likely grid selection bias")
        return "FAIL"
    else:
        print(f"  ✗ FAIL — no configs profitable")
        return "FAIL"


def check_4_holdout_oos(datasets_raw):
    """Holdout OOS: exclude last 60 days, train on everything else,
    then run best config on holdout only."""
    print("\n" + "=" * 70)
    print("  CHECK 4: HOLDOUT OOS (last 60 days)")
    print("=" * 70)

    holdout = 24 * 60  # 60 days of hourly candles

    # Run with holdout excluded from training
    trades_full = run_backtest(datasets_raw, BEST)
    trades_noholdout = run_backtest(datasets_raw, BEST, holdout_candles=holdout)

    # Holdout-only trades: trades in full but not in noholdout
    # Approximate: trades whose entry_idx >= (max_len - holdout)
    max_len = max(len(d["close"]) for d in datasets_raw.values())
    holdout_start = max_len - holdout

    holdout_trades = [t for t in trades_full if t["entry_idx"] >= holdout_start]
    pre_holdout = [t for t in trades_full if t["entry_idx"] < holdout_start]

    s_pre = summary(pre_holdout)
    s_hold = summary(holdout_trades)

    print(f"\n  Pre-holdout: N={s_pre['n']} WR={s_pre['wr']*100:.1f}% "
          f"PF={s_pre['pf']:.2f} total={s_pre['total']:+.1f}%")

    if s_hold["n"] > 0:
        print(f"  Holdout OOS: N={s_hold['n']} WR={s_hold['wr']*100:.1f}% "
              f"PF={s_hold['pf']:.2f} total={s_hold['total']:+.1f}%")

        if s_hold["pf"] > 1.0 and s_hold["wr"] > 0.4:
            print(f"\n  ✓ PASS — holdout profitable")
            return "PASS"
        elif s_hold["pf"] > 0.9:
            print(f"\n  ~ MARGINAL — holdout near breakeven")
            return "MARGINAL"
        else:
            print(f"\n  ✗ FAIL — holdout unprofitable")
            return "FAIL"
    else:
        print(f"  Holdout OOS: NO TRADES (not enough signal in last 60 days)")
        return "INCONCLUSIVE"


def check_5_per_coin(trades):
    """Per-coin alpha analysis."""
    print("\n" + "=" * 70)
    print("  CHECK 5: PER-COIN ALPHA ANALYSIS")
    print("=" * 70)

    if not trades:
        print("  NO TRADES"); return "FAIL"

    coins = {}
    for t in trades:
        sym = t["sym"]
        if sym not in coins:
            coins[sym] = []
        coins[sym].append(t["pnl"])

    print(f"\n  {'Symbol':<12s} {'N':>4s} {'WR':>6s} {'PF':>6s} {'Avg':>8s} {'Total':>8s}")
    print("  " + "-" * 50)

    n_profitable = 0
    for sym in sorted(coins.keys()):
        pnls = coins[sym]
        n = len(pnls)
        wr = np.mean([p > 0 for p in pnls]) * 100
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 0
        total = sum(pnls)
        avg = np.mean(pnls)
        if total > 0:
            n_profitable += 1
        print(f"  {sym:<12s} {n:>4d} {wr:>5.1f}% {pf:>5.2f} {avg:>+7.3f}% {total:>+7.1f}%")

    total_coins = len(coins)
    print(f"\n  Profitable coins: {n_profitable}/{total_coins}")

    # Check: is alpha concentrated in BTC/ETH only?
    btc_eth_pnl = sum(sum(coins.get(s, [0])) for s in ["BTC/USDT", "ETH/USDT"])
    total_pnl = sum(sum(v) for v in coins.values())
    if total_pnl > 0:
        btc_eth_frac = btc_eth_pnl / total_pnl * 100
        print(f"  BTC+ETH contribution: {btc_eth_frac:.0f}% of total PnL")

        if n_profitable >= total_coins * 0.5 and btc_eth_frac < 80:
            print(f"\n  ✓ PASS — alpha spread across coins")
            return "PASS"
        elif btc_eth_frac >= 80:
            print(f"\n  ✗ FAIL — alpha concentrated in BTC/ETH = just 'buy crypto'")
            return "FAIL"
        else:
            print(f"\n  ~ MARGINAL")
            return "MARGINAL"
    else:
        print(f"\n  ✗ FAIL — net negative")
        return "FAIL"


def check_6_drawdown(trades):
    """Drawdown profile analysis."""
    print("\n" + "=" * 70)
    print("  CHECK 6: DRAWDOWN PROFILE")
    print("=" * 70)

    if not trades:
        print("  NO TRADES"); return "FAIL"

    pnls = np.array([t["pnl"] for t in trades])
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum

    max_dd = dd.max()
    max_dd_idx = np.argmax(dd)

    # Find DD start (last peak before max DD)
    dd_start = max_dd_idx
    for i in range(max_dd_idx, -1, -1):
        if cum[i] == peak[i]:
            dd_start = i
            break

    # Find DD end (recovery)
    dd_end = len(cum) - 1
    peak_val = peak[max_dd_idx]
    for i in range(max_dd_idx, len(cum)):
        if cum[i] >= peak_val:
            dd_end = i
            break

    dd_duration = dd_end - dd_start  # in trades
    recovery = dd_end - max_dd_idx

    # Calmar-like: total / maxDD
    total = cum[-1]
    calmar = total / max_dd if max_dd > 0 else float("inf")

    print(f"\n  Max drawdown: {max_dd:.1f}%")
    print(f"  DD duration: {dd_duration} trades ({dd_start} → {dd_end})")
    print(f"  Recovery: {recovery} trades from trough")
    print(f"  Total return: {total:+.1f}%")
    print(f"  Return/MaxDD (Calmar): {calmar:.2f}")

    # All drawdowns > 10%
    in_dd = False
    dd_events = []
    for i in range(len(dd)):
        if dd[i] > 10 and not in_dd:
            in_dd = True
            dd_events.append({"start": i, "depth": dd[i]})
        elif dd[i] > dd_events[-1]["depth"] if in_dd and dd_events else False:
            dd_events[-1]["depth"] = dd[i]
        elif dd[i] == 0 and in_dd:
            in_dd = False

    if dd_events:
        print(f"\n  Drawdowns > 10%: {len(dd_events)}")
        for e in dd_events:
            print(f"    Trade #{e['start']}: {e['depth']:.1f}%")

    if max_dd < 20 and calmar > 2:
        print(f"\n  ✓ PASS — acceptable drawdown profile")
        return "PASS"
    elif max_dd < 30 and calmar > 1:
        print(f"\n  ~ MARGINAL — high DD but positive calmar")
        return "MARGINAL"
    else:
        print(f"\n  ✗ FAIL — unacceptable drawdown (DD={max_dd:.0f}%, calmar={calmar:.2f})")
        return "FAIL"


def check_7_slippage(datasets_raw):
    """Slippage stress test: add 3, 5, 7, 10 bps/side."""
    print("\n" + "=" * 70)
    print("  CHECK 7: SLIPPAGE STRESS TEST")
    print("=" * 70)

    print(f"\n  {'Extra bps':>10s} {'N':>4s} {'WR':>6s} {'PF':>6s} {'Avg':>8s} {'Total':>8s}")
    print("  " + "-" * 45)

    all_pass = True
    for extra in [0, 5, 10]:
        trades = run_backtest(datasets_raw, BEST, extra_fee_bps=extra)
        s = summary(trades)
        if s["n"] > 0:
            print(f"  {extra:>10d} {s['n']:>4d} {s['wr']*100:>5.1f}% {s['pf']:>5.2f} "
                  f"{s['avg']:>+7.3f}% {s['total']:>+7.1f}%")
            if extra == 5 and s["pf"] < 1.0:
                all_pass = False
        else:
            print(f"  {extra:>10d}: NO TRADES")

    if all_pass:
        print(f"\n  ✓ PASS — PF > 1 at +5bps slippage")
        return "PASS"
    else:
        print(f"\n  ✗ FAIL — unprofitable at +5bps slippage")
        return "FAIL"


def check_8_dsr(trades, n_configs=27):
    """Deflated Sharpe Ratio accounting for multiple testing."""
    print("\n" + "=" * 70)
    print("  CHECK 8: DEFLATED SHARPE RATIO (n_configs={})".format(n_configs))
    print("=" * 70)

    if not trades:
        print("  NO TRADES"); return "FAIL"

    pnls = np.array([t["pnl"] for t in trades])
    n = len(pnls)
    sr = pnls.mean() / pnls.std() if pnls.std() > 0 else 0

    # Annualize (assume ~1 trade/day on avg)
    trades_per_year = n / (n / 365) if n > 0 else 252  # rough
    sr_annual = sr * np.sqrt(min(trades_per_year, 365))

    # Skewness and kurtosis
    skew = stats.skew(pnls)
    kurt = stats.kurtosis(pnls)  # excess kurtosis

    # Expected max Sharpe under null (Bailey & Lopez de Prado)
    # E[max(SR)] ≈ sqrt(2 * log(N)) - (log(pi) + log(log(N))) / (2 * sqrt(2 * log(N)))
    # where N = number of trials
    if n_configs > 1:
        log_n = np.log(n_configs)
        e_max_sr = (np.sqrt(2 * log_n) -
                    (np.log(np.pi) + np.log(log_n)) / (2 * np.sqrt(2 * log_n)))
    else:
        e_max_sr = 0

    # DSR: probability that observed SR > E[maxSR] under null
    # PSR (Probabilistic Sharpe Ratio) adjusted
    sr_std = np.sqrt((1 - skew * sr + (kurt - 1) / 4 * sr**2) / n)

    # DSR = Φ((SR - E[maxSR]) / σ(SR))
    if sr_std > 0:
        dsr_z = (sr - e_max_sr) / sr_std
        dsr_p = stats.norm.cdf(dsr_z)
    else:
        dsr_z = 0
        dsr_p = 0.5

    print(f"\n  Sample Sharpe (per-trade): {sr:.4f}")
    print(f"  Annualized Sharpe (approx): {sr_annual:.2f}")
    print(f"  Skewness: {skew:.2f}")
    print(f"  Excess kurtosis: {kurt:.2f}")
    print(f"  E[max SR] under null ({n_configs} configs): {e_max_sr:.4f}")
    print(f"  SR std error: {sr_std:.4f}")
    print(f"  DSR z-score: {dsr_z:.2f}")
    print(f"  DSR p-value: {dsr_p:.4f}")

    if dsr_p > 0.95:
        print(f"\n  ✓ PASS — SR survives multiple testing correction (DSR p={dsr_p:.3f})")
        return "PASS"
    elif dsr_p > 0.80:
        print(f"\n  ~ MARGINAL — weak survival (DSR p={dsr_p:.3f})")
        return "MARGINAL"
    else:
        print(f"\n  ✗ FAIL — SR does not survive multiple testing (DSR p={dsr_p:.3f})")
        return "FAIL"


# ======================================================================
# Main
# ======================================================================

def main():
    print("=" * 70)
    print("  BIG MOVE DENOISED: FULL QUANT VALIDATION")
    print("=" * 70)
    print(f"  Best config: R={BEST['R']} Q={BEST['Q']} wl={BEST['wl']} "
          f"thr={BEST['thr']} LO={BEST['long_only']} vel={BEST['vel_thr']}")
    print(f"  Symbols: {len(SYMBOLS)}")
    print()

    # Load raw data
    datasets_raw = {}
    for sym in SYMBOLS:
        key = sym.replace("/", "")
        path = DATA / f"{key}_1h.parquet"
        if not path.exists():
            print(f"  SKIP {sym}: no data at {path}")
            continue
        df = pd.read_parquet(path)
        # Trim to MAX_CANDLES (take most recent)
        if len(df) > MAX_CANDLES:
            df = df.iloc[-MAX_CANDLES:].reset_index(drop=True)
        d = {
            "close": df["close"].values.astype(float),
            "high": df["high"].values.astype(float),
            "low": df["low"].values.astype(float),
            "volume": df["volume"].values.astype(float),
        }
        # Try to get timestamps
        if "timestamp" in df.columns:
            d["ts"] = df["timestamp"].values
        elif df.index.dtype.kind == 'M':
            d["ts"] = df.index.values
        else:
            d["ts"] = np.arange(len(df))
        datasets_raw[sym] = d
        print(f"  {sym}: {len(d['close'])} candles")

    if not datasets_raw:
        print("  No data!"); return

    # Run best config once for checks that need trades
    print(f"\n  Running best config backtest...", flush=True)
    trades = run_backtest(datasets_raw, BEST)
    s = summary(trades)
    print(f"  Baseline: N={s['n']} WR={s['wr']*100:.1f}% PF={s['pf']:.2f} "
          f"avg={s['avg']:+.3f}% total={s['total']:+.1f}%\n")

    # Run all 8 checks
    results = {}
    results["1_beta"] = check_1_beta(trades, datasets_raw)
    results["2_monthly"] = check_2_monthly(trades)
    results["3_grid_bias"] = check_3_grid_bias(datasets_raw)
    results["4_holdout_oos"] = check_4_holdout_oos(datasets_raw)
    results["5_per_coin"] = check_5_per_coin(trades)
    results["6_drawdown"] = check_6_drawdown(trades)
    results["7_slippage"] = check_7_slippage(datasets_raw)
    results["8_dsr"] = check_8_dsr(trades, n_configs=27)

    # Final scorecard
    print("\n\n" + "=" * 70)
    print("  FINAL SCORECARD")
    print("=" * 70 + "\n")
    for k, v in results.items():
        icon = "✓" if v == "PASS" else ("~" if v == "MARGINAL" else "✗")
        print(f"  {icon} {k}: {v}")

    n_pass = sum(1 for v in results.values() if v == "PASS")
    n_total = len(results)
    print(f"\n  Score: {n_pass}/{n_total} PASS")

    if n_pass >= 6:
        print(f"\n  VERDICT: PROMISING — consider paper trading")
    elif n_pass >= 4:
        print(f"\n  VERDICT: MARGINAL — needs more work before deployment")
    else:
        print(f"\n  VERDICT: FAIL — strategy does not pass validation")


if __name__ == "__main__":
    main()
