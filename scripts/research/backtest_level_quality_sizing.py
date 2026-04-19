"""
Backtest: Level Quality + Position Sizing improvements to Miro.

Two independent experiments on top of Miro d1_only + ML:

A) Per-touch level quality:
   - For each touch of a level: volume, wick rejection, bounce speed
   - Aggregate into quality features, add to ML
   - Hypothesis: better level quality -> higher WR

B) Position sizing by P(big_move):
   - Variable risk: 2-5% depending on P(big_move)
   - Same trades, but bigger when timing is right
   - Hypothesis: same WR, higher annual from better capital allocation

Usage:
    python scripts/research/backtest_level_quality_sizing.py
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

from src.strategy.levels import get_rolling_levels, find_swing_points, cluster_points
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
# Per-touch level quality
# ======================================================================

def compute_level_quality(df: pd.DataFrame, level: Level, current_idx: int,
                          lookback: int = 200, swing_order: int = 5) -> dict:
    """Compute quality features for a level based on its touches.

    For each swing point that formed this level, analyze:
    - Volume at touch vs average
    - Wick rejection strength (how much wick vs body)
    - Bounce speed (candles to move 1% away from level)
    - Age spread (distance between first and last touch)
    """
    start = max(0, current_idx - lookback)
    end = current_idx - swing_order

    # Re-find swing points that contributed to this level
    points = find_swing_points(df, start, end, order=swing_order)
    tolerance = level.zone_high - level.zone_low
    if tolerance <= 0:
        tolerance = level.price * 0.01

    # Filter points that belong to this level
    level_touches = []
    for idx, price, ptype in points:
        if abs(price - level.price) <= tolerance * 1.2:
            level_touches.append((idx, price, ptype))

    if not level_touches:
        return {}

    f = {}
    n = len(level_touches)
    f["lq_num_touches"] = float(n)

    # Volume at each touch
    touch_vol_ratios = []
    for idx, price, ptype in level_touches:
        if idx < 30 or idx >= len(df):
            continue
        avg_vol = df["volume"].iloc[max(0, idx - 30) : idx].mean()
        if avg_vol > 0:
            touch_vol_ratios.append(df["volume"].iloc[idx] / avg_vol)

    if touch_vol_ratios:
        f["lq_avg_touch_vol"] = float(np.mean(touch_vol_ratios))
        f["lq_max_touch_vol"] = float(np.max(touch_vol_ratios))
        f["lq_touch_vol_trend"] = float(
            touch_vol_ratios[-1] / touch_vol_ratios[0]
        ) if touch_vol_ratios[0] > 0 else 1.0
    else:
        f["lq_avg_touch_vol"] = 1.0
        f["lq_max_touch_vol"] = 1.0
        f["lq_touch_vol_trend"] = 1.0

    # Wick rejection at each touch
    wick_rejections = []
    for idx, price, ptype in level_touches:
        if idx >= len(df):
            continue
        candle_range = df["high"].iloc[idx] - df["low"].iloc[idx]
        if candle_range <= 0:
            continue
        if ptype == "low":
            # Lower wick = rejection of selling pressure
            wick = min(df["close"].iloc[idx], df["open"].iloc[idx]) - df["low"].iloc[idx]
        else:
            # Upper wick = rejection of buying pressure
            wick = df["high"].iloc[idx] - max(df["close"].iloc[idx], df["open"].iloc[idx])
        wick_rejections.append(wick / candle_range)

    if wick_rejections:
        f["lq_avg_wick_rejection"] = float(np.mean(wick_rejections))
        f["lq_max_wick_rejection"] = float(np.max(wick_rejections))
    else:
        f["lq_avg_wick_rejection"] = 0.0
        f["lq_max_wick_rejection"] = 0.0

    # Bounce speed: candles to move 1% away from level after touch
    bounce_speeds = []
    for idx, price, ptype in level_touches:
        target = level.price * 1.01 if ptype == "low" else level.price * 0.99
        for j in range(idx + 1, min(idx + 20, len(df))):
            if ptype == "low" and df["close"].iloc[j] >= target:
                bounce_speeds.append(j - idx)
                break
            elif ptype == "high" and df["close"].iloc[j] <= target:
                bounce_speeds.append(j - idx)
                break
        else:
            bounce_speeds.append(20)  # didn't bounce fast

    if bounce_speeds:
        f["lq_avg_bounce_speed"] = float(np.mean(bounce_speeds))
        f["lq_fast_bounces"] = float(sum(1 for s in bounce_speeds if s <= 3) / len(bounce_speeds))
    else:
        f["lq_avg_bounce_speed"] = 10.0
        f["lq_fast_bounces"] = 0.0

    # Touch spacing (wider = more significant)
    if n >= 2:
        touch_indices = sorted([t[0] for t in level_touches])
        gaps = [touch_indices[i+1] - touch_indices[i] for i in range(len(touch_indices)-1)]
        f["lq_avg_touch_gap"] = float(np.mean(gaps))
        f["lq_age_span"] = float(touch_indices[-1] - touch_indices[0])
    else:
        f["lq_avg_touch_gap"] = 0.0
        f["lq_age_span"] = 0.0

    # Zone tightness: how precisely price respects the level
    touch_prices = [t[1] for t in level_touches]
    if len(touch_prices) >= 2:
        f["lq_zone_tightness"] = float(np.std(touch_prices) / level.price * 100)
    else:
        f["lq_zone_tightness"] = 0.0

    return f


# ======================================================================
# Big Move features (minimal, for position sizing)
# ======================================================================

def compute_bigmove_prob_simple(df_1h: pd.DataFrame, target_ts) -> float:
    """Simple big-move probability estimate without full ML model.

    Uses the top 3 discriminators found in research:
    volatility_contraction, volume_spike, atr_pct.
    Returns 0-1 score.
    """
    if df_1h is None or df_1h.empty:
        return 0.0

    mask = df_1h["ts"] <= target_ts
    if mask.sum() < 50:
        return 0.0

    idx = mask.sum() - 1
    lookback = 24
    if idx < lookback + 10:
        return 0.0

    window = df_1h.iloc[idx - lookback : idx]
    close = df_1h["close"].iloc[idx]

    signals = 0.0
    total_weight = 0.0

    # 1. Volatility contraction (squeeze)
    if idx >= 40:
        std_20 = df_1h["close"].iloc[idx - 20 : idx].std()
        std_prev = df_1h["close"].iloc[idx - 40 : idx - 20].std()
        if std_prev > 0:
            contraction = std_20 / std_prev
            # < 0.7 = strong squeeze
            if contraction < 0.5:
                signals += 3.0
            elif contraction < 0.7:
                signals += 2.0
            elif contraction < 0.85:
                signals += 1.0
        total_weight += 3.0

    # 2. Volume spike
    avg_vol = df_1h["volume"].iloc[max(0, idx - lookback * 3) : idx - lookback].mean()
    if avg_vol > 0:
        vol_spike = window["volume"].max() / avg_vol
        if vol_spike > 3.0:
            signals += 3.0
        elif vol_spike > 2.0:
            signals += 2.0
        elif vol_spike > 1.5:
            signals += 1.0
    total_weight += 3.0

    # 3. ATR elevation
    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - window["close"].shift(1)).abs(),
        (window["low"] - window["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_pct = float(tr.mean() / close * 100)
    if atr_pct > 2.0:
        signals += 2.0
    elif atr_pct > 1.5:
        signals += 1.0
    total_weight += 2.0

    # 4. Volume trend (accumulation)
    split = lookback // 4
    vol_recent = window["volume"].iloc[-split:].mean()
    vol_earlier = window["volume"].iloc[:-split].mean()
    if vol_earlier > 0 and vol_recent / vol_earlier > 1.5:
        signals += 2.0
    total_weight += 2.0

    return signals / total_weight if total_weight > 0 else 0.0


# ======================================================================
# Core backtest
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


def collect_trades(entry_data, d1_data, data_1h, params, compute_quality=False):
    """Walk-forward: collect Miro d1_only trades with optional quality + bigmove features."""
    all_records = []

    for symbol in SYMBOLS:
        if symbol not in entry_data or symbol not in d1_data:
            continue
        df = entry_data[symbol]
        d1_df = d1_data[symbol]
        df_1h = data_1h.get(symbol) if data_1h else None

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
                        "bigmove_score": at.get("bigmove_score", 0.0),
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

                # Level quality features
                if compute_quality:
                    # Use entry TF (4h) for quality on D1 levels
                    lq = compute_level_quality(df, lv, i,
                                               lookback=params["level_lookback"],
                                               swing_order=5)
                    # Also compute quality from D1 perspective
                    if d1_df is not None:
                        d1_mask = d1_df["ts"] <= ts
                        d1_idx = d1_mask.sum() - 1
                        if d1_idx > 50:
                            lq_d1 = compute_level_quality(d1_df, lv, d1_idx,
                                                          lookback=120, swing_order=3)
                            # Prefix D1 quality features
                            for k, v in lq_d1.items():
                                features[f"d1_{k}"] = v
                    features.update(lq)

                # Big move score for position sizing
                bigmove_score = 0.0
                if df_1h is not None:
                    bigmove_score = compute_bigmove_prob_simple(df_1h, ts)
                features["bigmove_score"] = bigmove_score

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "features": features,
                    "bigmove_score": bigmove_score,
                }

    return pd.DataFrame(all_records)


# ======================================================================
# ML Analysis
# ======================================================================

def run_ml_analysis(trades_df, name, period_months, feature_prefix_filter=None):
    """Run ML analysis. Returns (results_list, y_prob, clf, feature_cols)."""
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()

    if len(df) < 20:
        print(f"  {name}: Too few trades ({len(df)})")
        return [], None, None, None

    df["target"] = (df["outcome"] == "win").astype(int)

    exclude = {"outcome", "target", "pnl_pct", "symbol", "signal_type",
               "hold_candles", "bigmove_score", "d1_aligned"}
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
        risk_pct = 3.0
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
    print(f"\n  Top 10 features: ", end="")
    print(", ".join(f"{n}={v:.3f}" for n, v in fi[:10]))

    # Show specific feature group importances
    if feature_prefix_filter:
        group_fi = [(n, v) for n, v in fi if any(n.startswith(p) for p in feature_prefix_filter)]
        if group_fi:
            total_imp = sum(v for _, v in group_fi)
            print(f"  Quality features ({len(group_fi)}): total importance {total_imp:.3f}")
            for n, v in group_fi[:8]:
                print(f"    {n:<30} {v:.4f}")

    return results, y_prob, clf, feature_cols


# ======================================================================
# Position sizing experiment
# ======================================================================

def run_position_sizing(trades_df, y_prob, ml_threshold, period_months):
    """Compare flat sizing vs variable sizing based on bigmove_score."""
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    if len(df) < 20:
        return

    df["target"] = (df["outcome"] == "win").astype(int)
    mask = y_prob >= ml_threshold
    df_filtered = df[mask].copy()

    if len(df_filtered) < 10:
        print(f"  Not enough trades after ML filter (thr={ml_threshold})")
        return

    print(f"\n  Position sizing analysis ({len(df_filtered)} trades, ML thr={ml_threshold}):")

    # Strategies for position sizing
    configs = [
        ("Flat 3%", lambda bm: 3.0),
        ("Flat 4%", lambda bm: 4.0),
        ("BM adaptive 2-4%", lambda bm: 2.0 + bm * 2.0),
        ("BM adaptive 2-5%", lambda bm: 2.0 + bm * 3.0),
        ("BM adaptive 3-5%", lambda bm: 3.0 + bm * 2.0),
        ("BM step: 3%/<0.5, 5%/>=0.5", lambda bm: 5.0 if bm >= 0.5 else 3.0),
    ]

    print(f"  {'Strategy':<35} {'AvgRisk':>8} {'Monthly':>8} {'Annual':>8} {'MaxDD':>7} {'Sharpe':>7}")

    for name, size_fn in configs:
        pnls = df_filtered["pnl_pct"].values
        bm_scores = df_filtered["bigmove_score"].values
        target = df_filtered["target"].values
        tpm = len(df_filtered) / period_months

        # Compute per-trade P&L with variable sizing
        sized_returns = []
        risks = []
        for pnl, bm in zip(pnls, bm_scores):
            risk = size_fn(bm)
            risks.append(risk)
            sized_returns.append(pnl * risk / 100)

        sized_returns = np.array(sized_returns)
        avg_risk = np.mean(risks)

        # Monthly return
        monthly_ret = np.mean(sized_returns) * tpm

        # Annual return
        annual = (1 + monthly_ret) ** 12 - 1

        # Max drawdown
        equity = np.cumprod(1 + sized_returns / 100)
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak
        max_dd = dd.max()

        # Sharpe (monthly, annualized)
        monthly_returns = []
        chunk = max(1, int(tpm))
        for j in range(0, len(sized_returns), chunk):
            monthly_returns.append(sized_returns[j:j+chunk].sum())
        if len(monthly_returns) > 1:
            sharpe = np.mean(monthly_returns) / np.std(monthly_returns) * np.sqrt(12)
        else:
            sharpe = 0

        print(f"  {name:<35} {avg_risk:>7.1f}% {monthly_ret*100:>+7.2f}% "
              f"{annual*100:>+7.1f}% {max_dd*100:>6.1f}% {sharpe:>6.2f}")


# ======================================================================
# Main
# ======================================================================

def main():
    print("=== Level Quality + Position Sizing Experiments ===\n")

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    data_1h = load_data("1h")

    print(f"D1: {len(d1_data)}, 4H: {len(data_4h)}, 1H: {len(data_1h)} symbols")

    period_months = 6.8
    params = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150,
    )

    # ============================================================
    # A) Baseline: Miro d1_only (no quality, flat sizing)
    # ============================================================
    print("\n" + "=" * 70)
    print("  A) BASELINE: Miro d1_only + ML (flat 3% risk)")
    print("=" * 70)

    print("  Collecting baseline trades...")
    baseline_df = collect_trades(data_4h, d1_data, data_1h, params,
                                  compute_quality=False)
    print(f"  Total: {len(baseline_df)} trades")

    base_results, base_yprob, _, _ = run_ml_analysis(
        baseline_df, "Baseline", period_months)

    # ============================================================
    # B) Miro + Level Quality features
    # ============================================================
    print("\n" + "=" * 70)
    print("  B) EXPERIMENT: Miro d1_only + Level Quality features in ML")
    print("=" * 70)

    print("  Collecting trades with level quality...")
    quality_df = collect_trades(data_4h, d1_data, data_1h, params,
                                 compute_quality=True)
    print(f"  Total: {len(quality_df)} trades")

    quality_results, quality_yprob, _, _ = run_ml_analysis(
        quality_df, "Miro + LevelQuality", period_months,
        feature_prefix_filter=["lq_", "d1_lq_"])

    # ============================================================
    # C) Position sizing by P(big_move)
    # ============================================================
    print("\n" + "=" * 70)
    print("  C) EXPERIMENT: Variable position sizing by bigmove_score")
    print("=" * 70)

    # Use baseline trades with bigmove_score
    if base_yprob is not None and base_results:
        best_thr = max(base_results, key=lambda r: r["annual"])["thr"]
        run_position_sizing(baseline_df, base_yprob, best_thr, period_months)

    # ============================================================
    # D) Combined: Level Quality + Position Sizing
    # ============================================================
    if quality_results and quality_yprob is not None:
        print("\n" + "=" * 70)
        print("  D) COMBINED: Level Quality ML + Variable sizing")
        print("=" * 70)

        best_thr_q = max(quality_results, key=lambda r: r["annual"])["thr"]
        run_position_sizing(quality_df, quality_yprob, best_thr_q, period_months)

    # ============================================================
    # SUMMARY
    # ============================================================
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")

    comparisons = []
    if base_results:
        best_b = max(base_results, key=lambda r: r["annual"])
        best_b["name"] = "A) Baseline"
        comparisons.append(best_b)

    if quality_results:
        best_q = max(quality_results, key=lambda r: r["annual"])
        best_q["name"] = "B) + LevelQuality"
        comparisons.append(best_q)

    if comparisons:
        print(f"  {'Config':<25} {'Thr':>5} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Annual':>8}")
        for s in sorted(comparisons, key=lambda x: x["annual"], reverse=True):
            print(f"  {s['name']:<25} {s['thr']:>5.2f} {s['n']:>5} {s['tpm']:>4.0f} "
                  f"{s['wr']*100:>5.1f}% {s['pf']:>5.2f} "
                  f"{s['exp']*100:>+8.3f}% {s['annual']*100:>+7.1f}%")

        if len(comparisons) >= 2:
            delta = (comparisons[0]["annual"] - comparisons[-1]["annual"]) * 100
            print(f"\n  Level Quality delta: {delta:+.1f}pp annual")


if __name__ == "__main__":
    main()
