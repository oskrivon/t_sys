"""
Backtest: Swing Structure + Hourly Bias + Coin-in-Play scoring.

Remaining Etap 3 items, tested on top of current best (d1_only + LQ + ML).

A) Swing structure: HH/HL/LH/LL detector. Trade only in direction of structure.
B) Hourly bias: hour_utc as feature (12-13 UTC found best in research).
C) Coin-in-play: volume_ratio + abs_move_7d composite. Trade only top coins.
D) All combined.

Usage:
    python scripts/research/backtest_swing_hour_cip.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import cross_val_predict

from src.strategy.levels import get_rolling_levels, find_swing_points
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

# Reuse level quality from previous experiment
from backtest_level_quality_sizing import compute_level_quality

DATA = ROOT / "data" / "processed" / "candles"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10


# ======================================================================
# Swing Structure: HH/HL/LH/LL detector
# ======================================================================

def detect_swing_structure(df: pd.DataFrame, idx: int, order: int = 5,
                           n_swings: int = 6) -> dict:
    """Detect market structure (HH/HL/LH/LL) at given index.

    Returns dict of structure features.
    """
    f = {}

    # Find recent swing points
    start = max(0, idx - 200)
    end = idx - order
    if end - start < 20:
        f["sw_trend"] = 0.0
        f["sw_hh_count"] = 0.0
        f["sw_ll_count"] = 0.0
        f["sw_consistency"] = 0.0
        f["sw_last_swing_type"] = 0.0
        f["sw_swing_range_pct"] = 0.0
        return f

    highs = []
    lows = []
    for i in range(max(start, order), min(end, len(df) - order)):
        window_h = df["high"].iloc[i - order : i + order + 1]
        if df["high"].iloc[i] == window_h.max():
            highs.append((i, df["high"].iloc[i]))

        window_l = df["low"].iloc[i - order : i + order + 1]
        if df["low"].iloc[i] == window_l.min():
            lows.append((i, df["low"].iloc[i]))

    # Take last N swings
    highs = highs[-n_swings:]
    lows = lows[-n_swings:]

    # Count HH/LH and HL/LL
    hh_count, lh_count = 0, 0
    for i in range(1, len(highs)):
        if highs[i][1] > highs[i-1][1]:
            hh_count += 1
        else:
            lh_count += 1

    hl_count, ll_count = 0, 0
    for i in range(1, len(lows)):
        if lows[i][1] > lows[i-1][1]:
            hl_count += 1
        else:
            ll_count += 1

    total_h = hh_count + lh_count
    total_l = hl_count + ll_count

    # Trend: +1 = uptrend (HH+HL), -1 = downtrend (LH+LL), 0 = mixed
    bullish_signals = hh_count + hl_count
    bearish_signals = lh_count + ll_count
    total_signals = bullish_signals + bearish_signals

    if total_signals > 0:
        trend_score = (bullish_signals - bearish_signals) / total_signals
    else:
        trend_score = 0.0

    f["sw_trend"] = trend_score  # -1 to +1
    f["sw_hh_count"] = float(hh_count)
    f["sw_ll_count"] = float(ll_count)

    # Consistency: how clean is the structure? (all HH+HL or all LH+LL)
    if total_signals > 0:
        f["sw_consistency"] = max(bullish_signals, bearish_signals) / total_signals
    else:
        f["sw_consistency"] = 0.0

    # Last swing direction
    all_swings = [(i, p, "h") for i, p in highs] + [(i, p, "l") for i, p in lows]
    all_swings.sort(key=lambda x: x[0])
    if all_swings:
        last_type = all_swings[-1][2]
        f["sw_last_swing_type"] = 1.0 if last_type == "h" else -1.0
    else:
        f["sw_last_swing_type"] = 0.0

    # Swing range (current price position within recent swing range)
    if highs and lows:
        swing_high = max(h[1] for h in highs[-3:]) if len(highs) >= 3 else highs[-1][1]
        swing_low = min(l[1] for l in lows[-3:]) if len(lows) >= 3 else lows[-1][1]
        swing_range = swing_high - swing_low
        close = df["close"].iloc[idx]
        if swing_range > 0:
            f["sw_swing_range_pct"] = swing_range / close * 100
            f["sw_position_in_range"] = (close - swing_low) / swing_range
        else:
            f["sw_swing_range_pct"] = 0.0
            f["sw_position_in_range"] = 0.5
    else:
        f["sw_swing_range_pct"] = 0.0
        f["sw_position_in_range"] = 0.5

    return f


# ======================================================================
# Coin-in-Play scoring
# ======================================================================

def compute_coin_in_play_score(df: pd.DataFrame, idx: int) -> dict:
    """Compute coin-in-play features at candle idx.

    Based on volume activity and price momentum — is this coin "in play"?
    """
    f = {}

    close = df["close"].iloc[idx]

    # Volume ratio (30-bar)
    if idx >= 30:
        avg_vol = df["volume"].iloc[idx - 30 : idx].mean()
        f["cip_volume_ratio_30"] = float(df["volume"].iloc[idx] / avg_vol) if avg_vol > 0 else 1.0
        # Volume ratio (7-day equivalent)
        if idx >= 42:
            avg_vol_7d = df["volume"].iloc[idx - 42 : idx].mean()
            recent_vol = df["volume"].iloc[idx - 6 : idx].mean()
            f["cip_vol_ratio_7d"] = float(recent_vol / avg_vol_7d) if avg_vol_7d > 0 else 1.0
        else:
            f["cip_vol_ratio_7d"] = 1.0
    else:
        f["cip_volume_ratio_30"] = 1.0
        f["cip_vol_ratio_7d"] = 1.0

    # Absolute price move (7d and 30d)
    if idx >= 42:
        f["cip_abs_move_7d"] = abs(close / df["close"].iloc[idx - 42] - 1) * 100
    else:
        f["cip_abs_move_7d"] = 0.0

    if idx >= 180:
        f["cip_abs_move_30d"] = abs(close / df["close"].iloc[idx - 180] - 1) * 100
    else:
        f["cip_abs_move_30d"] = 0.0

    # Range expansion: is recent range bigger than usual?
    if idx >= 42:
        recent_range = (df["high"].iloc[idx-6:idx].max() - df["low"].iloc[idx-6:idx].min()) / close * 100
        older_range = (df["high"].iloc[idx-42:idx-6].max() - df["low"].iloc[idx-42:idx-6].min()) / close * 100
        f["cip_range_expansion"] = float(recent_range / older_range) if older_range > 0 else 1.0
    else:
        f["cip_range_expansion"] = 1.0

    # Composite score (0-1)
    score = 0.0
    if f["cip_volume_ratio_30"] > 2.0: score += 0.3
    elif f["cip_volume_ratio_30"] > 1.5: score += 0.15
    if f["cip_abs_move_7d"] > 15: score += 0.3
    elif f["cip_abs_move_7d"] > 8: score += 0.15
    if f["cip_vol_ratio_7d"] > 1.5: score += 0.2
    if f["cip_range_expansion"] > 1.5: score += 0.2
    f["cip_composite"] = score

    return f


# ======================================================================
# Hourly bias features
# ======================================================================

def compute_hour_features(df: pd.DataFrame, idx: int) -> dict:
    """Hour-of-day features for the signal candle."""
    f = {}
    ts = df["ts"].iloc[idx]
    if hasattr(ts, "hour"):
        hour = ts.hour
        f["hr_hour"] = float(hour)
        # Peak hours bucket (from research: 12-13 UTC best)
        f["hr_is_peak"] = 1.0 if 11 <= hour <= 14 else 0.0
        # Session buckets
        f["hr_asia"] = 1.0 if 0 <= hour < 8 else 0.0
        f["hr_europe"] = 1.0 if 8 <= hour < 16 else 0.0
        f["hr_us"] = 1.0 if 16 <= hour < 24 else 0.0
    else:
        f["hr_hour"] = 12.0
        f["hr_is_peak"] = 0.0
        f["hr_asia"] = 0.0
        f["hr_europe"] = 1.0
        f["hr_us"] = 0.0
    return f


# ======================================================================
# Core backtest (builds on d1_only + LQ baseline)
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


def collect_trades(entry_data, d1_data, params,
                   use_swing=False, use_hours=False, use_cip=False,
                   use_lq=False):
    """Walk-forward trade collection with optional feature sets."""
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

                if use_lq:
                    lq = compute_level_quality(df, lv, i,
                                               lookback=params["level_lookback"],
                                               swing_order=5)
                    features.update(lq)
                    # D1 level quality
                    d1_mask = d1_df["ts"] <= ts
                    d1_idx = d1_mask.sum() - 1
                    if d1_idx > 50:
                        lq_d1 = compute_level_quality(d1_df, lv, d1_idx,
                                                      lookback=120, swing_order=3)
                        for k, v in lq_d1.items():
                            features[f"d1_{k}"] = v

                if use_swing:
                    sw = detect_swing_structure(df, i)
                    features.update(sw)
                    # Swing-direction alignment with signal
                    is_long = "long" in sig_type.value
                    features["sw_aligned"] = 1.0 if (
                        (is_long and sw["sw_trend"] > 0.3) or
                        (not is_long and sw["sw_trend"] < -0.3)
                    ) else 0.0

                if use_hours:
                    hr = compute_hour_features(df, i)
                    features.update(hr)

                if use_cip:
                    cip = compute_coin_in_play_score(df, i)
                    features.update(cip)

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "features": features,
                }

    return pd.DataFrame(all_records)


def run_ml(trades_df, name, period_months, risk_pct=4.0, feat_groups=None):
    """Run ML analysis, return best result dict."""
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    if len(df) < 20:
        print(f"  {name}: Too few trades ({len(df)})")
        return None

    df["target"] = (df["outcome"] == "win").astype(int)
    exclude = {"outcome", "target", "pnl_pct", "symbol", "signal_type",
               "hold_candles", "d1_aligned"}
    feature_cols = [c for c in df.columns if c not in exclude]

    X = df[feature_cols].fillna(0).values
    y = df["target"].values

    print(f"\n  {name}: {len(df)} trades ({y.sum()}W/{len(y)-y.sum()}L), "
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
                         "pf": pf, "exp": exp, "monthly": monthly, "annual": annual})

    # Feature importance
    clf.fit(X, y)
    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 12: ", end="")
    print(", ".join(f"{n}={v:.3f}" for n, v in fi[:12]))

    # Show specific feature groups
    if feat_groups:
        for prefix, label in feat_groups:
            group = [(n, v) for n, v in fi if n.startswith(prefix)]
            if group:
                total = sum(v for _, v in group)
                print(f"  {label} ({len(group)}): total {total:.3f} - ", end="")
                print(", ".join(f"{n}={v:.3f}" for n, v in group[:5]))

    if not results:
        return None
    best = max(results, key=lambda r: r["annual"])
    best["name"] = name
    return best


def main():
    print("=== Swing Structure + Hourly Bias + Coin-in-Play ===\n")

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    print(f"D1: {len(d1_data)}, 4H: {len(data_4h)} symbols")

    period_months = 6.8
    risk_pct = 4.0
    params = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150,
    )

    configs = [
        # (name, use_lq, use_swing, use_hours, use_cip, feat_groups)
        ("A) LQ baseline (current best)",
         True, False, False, False, [("lq_", "LQ"), ("d1_lq_", "D1-LQ")]),

        ("B) LQ + Swing Structure",
         True, True, False, False, [("sw_", "Swing")]),

        ("C) LQ + Hourly Bias",
         True, False, True, False, [("hr_", "Hour")]),

        ("D) LQ + Coin-in-Play",
         True, False, False, True, [("cip_", "CIP")]),

        ("E) LQ + Swing + Hour + CIP (all)",
         True, True, True, True,
         [("sw_", "Swing"), ("hr_", "Hour"), ("cip_", "CIP")]),
    ]

    summaries = []

    for name, use_lq, use_sw, use_hr, use_cip, fg in configs:
        print(f"\n{'='*70}")
        print(f"  {name}")
        print(f"{'='*70}")

        print("  Collecting trades...")
        trades = collect_trades(data_4h, d1_data, params,
                                use_swing=use_sw, use_hours=use_hr,
                                use_cip=use_cip, use_lq=use_lq)
        print(f"  Total: {len(trades)} trades")

        result = run_ml(trades, name, period_months, risk_pct, fg)
        if result:
            summaries.append(result)

    # Final comparison
    if summaries:
        print(f"\n{'='*70}")
        print(f"  COMPARISON (all at 4% risk)")
        print(f"{'='*70}")
        print(f"  {'Config':<40} {'Thr':>5} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Ann':>8}")
        for s in sorted(summaries, key=lambda x: x["annual"], reverse=True):
            print(f"  {s['name']:<40} {s['thr']:>5.2f} {s['n']:>5} {s['tpm']:>4.0f} "
                  f"{s['wr']*100:>5.1f}% {s['pf']:>5.2f} "
                  f"{s['exp']*100:>+8.3f}% {s['annual']*100:>+7.1f}%")

        base = next((s for s in summaries if "baseline" in s["name"]), summaries[-1])
        best = max(summaries, key=lambda x: x["annual"])
        if best["name"] != base["name"]:
            delta = (best["annual"] - base["annual"]) * 100
            print(f"\n  Best improvement: {best['name']}")
            print(f"  Delta vs baseline: {delta:+.1f}pp annual")
        else:
            print(f"\n  Baseline remains best. New features did not improve.")


if __name__ == "__main__":
    main()
