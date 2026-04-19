"""
Analysis: Can we pre-select profitable coins? + Vision impact estimate.

1. Coin selection: analyze which symbols profit in OOS, test if vol/momentum
   at signal time predicts profitable coins
2. Vision: estimate impact using prior research (75% WR at score>=7)

Uses the full walk-forward pipeline from backtest_walkforward_ml.py
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
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT",
    "TRX/USDT", "LINK/USDT", "ADA/USDT", "AVAX/USDT", "NEAR/USDT",
    "LTC/USDT", "FET/USDT", "UNI/USDT",
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "WIF/USDT", "ONDO/USDT", "RENDER/USDT",
    "ATOM/USDT", "ETC/USDT", "APT/USDT", "MANTA/USDT", "SEI/USDT",
    "JUP/USDT", "WLD/USDT", "STRK/USDT", "PENDLE/USDT", "ENA/USDT",
    "TAO/USDT", "GALA/USDT",
]

FEE_BPS = 10


def compute_level_quality_slim(df, level, current_idx, lookback=200, swing_order=5):
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
    touch_prices = [t[1] for t in level_touches]
    if len(touch_prices) >= 2:
        f["lq_zone_tightness"] = float(np.std(touch_prices) / level.price * 100)
    else:
        f["lq_zone_tightness"] = 0.0
    return f


def load_data(tf):
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


def collect_trades_with_coin_features(entry_data, d1_data, params):
    """Collect trades with extra coin-level features for selection analysis."""
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
                        "entry_ts": ts,
                    }
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

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
                lq = compute_level_quality_slim(df, lv, i, lookback=params["level_lookback"])
                features.update(lq)
                d1_mask = d1_df["ts"] <= ts
                d1_idx = d1_mask.sum() - 1
                if d1_idx > 50:
                    lq_d1 = compute_level_quality_slim(d1_df, lv, d1_idx, lookback=120, swing_order=3)
                    for k, v in lq_d1.items():
                        features[f"d1_{k}"] = v

                # Extra coin-level features for selection
                if i >= 42:
                    vol_7d = df["volume"].iloc[i-42:i].mean()
                    vol_30d = df["volume"].iloc[max(0,i-180):i].mean() if i >= 180 else vol_7d
                    features["coin_vol_7d_usd"] = vol_7d * close_i  # proxy USD volume
                    features["coin_vol_trend"] = vol_7d / vol_30d if vol_30d > 0 else 1.0
                    features["coin_volatility_7d"] = float(df["close"].iloc[i-42:i].pct_change().std() * 100)
                    features["coin_abs_return_7d"] = abs(close_i / df["close"].iloc[i-42] - 1) * 100
                    features["coin_abs_return_30d"] = abs(close_i / df["close"].iloc[max(0,i-180)] - 1) * 100 if i >= 180 else 0

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "features": features,
                    "entry_ts": ts,
                }

    return pd.DataFrame(all_records)


def analyze_coin_selection(trades_df):
    """Can we predict which coins will be profitable?"""
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    df["target"] = (df["outcome"] == "win").astype(int)

    print(f"\n{'='*65}")
    print(f"  COIN SELECTION ANALYSIS")
    print(f"{'='*65}")
    print(f"  {len(df)} trades, {df['target'].sum()}W/{(1-df['target']).sum():.0f}L, WR {df['target'].mean()*100:.1f}%")

    # 1. Per-symbol profitability
    print(f"\n  Per-symbol (sorted by WR):")
    print(f"  {'Symbol':14s} {'N':>5} {'WR':>6} {'PnL':>8} {'AvgVol':>10} {'Vol7d':>8} {'AbsRet7d':>9}")
    sym_stats = []
    for sym in sorted(df["symbol"].unique()):
        s = df[df["symbol"] == sym]
        if len(s) < 5:
            continue
        wr = s["target"].mean()
        pnl = s["pnl_pct"].sum()
        avg_vol = s["coin_vol_7d_usd"].mean() if "coin_vol_7d_usd" in s.columns else 0
        vol_7d = s["coin_volatility_7d"].mean() if "coin_volatility_7d" in s.columns else 0
        abs_ret = s["coin_abs_return_7d"].mean() if "coin_abs_return_7d" in s.columns else 0
        sym_stats.append((sym, len(s), wr, pnl, avg_vol, vol_7d, abs_ret))

    sym_stats.sort(key=lambda x: x[2], reverse=True)
    for sym, n, wr, pnl, avg_vol, vol_7d, abs_ret in sym_stats:
        print(f"  {sym:14s} {n:>5} {wr*100:>5.0f}% {pnl:>+7.2f}% {avg_vol/1e6:>9.1f}M {vol_7d:>7.2f}% {abs_ret:>8.1f}%")

    # 2. Can coin features predict profitability?
    coin_features = ["coin_vol_7d_usd", "coin_vol_trend", "coin_volatility_7d",
                     "coin_abs_return_7d", "coin_abs_return_30d"]
    existing = [f for f in coin_features if f in df.columns]

    if existing:
        print(f"\n  Coin feature correlation with win:")
        for feat in existing:
            vals = df[feat].fillna(0)
            # Split into quartiles
            q1 = vals.quantile(0.25)
            q3 = vals.quantile(0.75)
            low = df[vals <= q1]["target"].mean()
            high = df[vals >= q3]["target"].mean()
            corr = vals.corr(df["target"])
            print(f"    {feat:25s}: corr={corr:+.3f}, WR(low25%)={low*100:.1f}%, WR(high25%)={high*100:.1f}%")

    # 3. Test coin filters on walk-forward
    print(f"\n  Coin filters (applied pre-ML):")
    risk_pct = 4.0
    total_days = (pd.to_datetime(df["entry_ts"]).max() - pd.to_datetime(df["entry_ts"]).min()).days
    period_months = total_days / 30

    filters = [
        ("No filter", lambda row: True),
        ("vol_7d > 2%", lambda row: row.get("coin_volatility_7d", 0) > 2.0),
        ("vol_7d > 3%", lambda row: row.get("coin_volatility_7d", 0) > 3.0),
        ("abs_ret_7d > 5%", lambda row: row.get("coin_abs_return_7d", 0) > 5.0),
        ("abs_ret_7d > 10%", lambda row: row.get("coin_abs_return_7d", 0) > 10.0),
        ("vol_trend > 1.2", lambda row: row.get("coin_vol_trend", 0) > 1.2),
        ("combo: vol>2% & ret>5%", lambda row: row.get("coin_volatility_7d", 0) > 2.0 and row.get("coin_abs_return_7d", 0) > 5.0),
    ]

    print(f"  {'Filter':30s} {'N':>6} {'WR':>6} {'PF':>6} {'Exp':>9} {'Ann':>8}")
    for name, fn in filters:
        mask = df.apply(fn, axis=1)
        sub = df[mask]
        if len(sub) < 20:
            continue
        wr = sub["target"].mean()
        fp = sub["pnl_pct"].values
        fy = sub["target"].values
        aw = fp[fy == 1].mean() if (fy == 1).sum() > 0 else 0
        al = abs(fp[fy == 0].mean()) if (fy == 0).sum() > 0 else 0
        exp = wr * aw - (1 - wr) * al
        gw = fp[fy == 1].sum() if (fy == 1).sum() > 0 else 0
        gl = abs(fp[fy == 0].sum()) if (fy == 0).sum() > 0 else 1
        pf = gw / gl if gl > 0 else 0
        tpm = len(sub) / period_months
        monthly = tpm * exp * risk_pct / 100
        annual = (1 + monthly) ** 12 - 1
        print(f"  {name:30s} {len(sub):>6} {wr*100:>5.1f}% {pf:>5.2f} {exp*100:>+8.3f}% {annual*100:>+7.1f}%")


def estimate_vision_impact(trades_df):
    """Estimate Claude Vision impact based on prior research."""
    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    df["target"] = (df["outcome"] == "win").astype(int)

    print(f"\n{'='*65}")
    print(f"  VISION IMPACT ESTIMATE")
    print(f"{'='*65}")

    total_days = (pd.to_datetime(df["entry_ts"]).max() - pd.to_datetime(df["entry_ts"]).min()).days
    period_months = total_days / 30
    risk_pct = 4.0
    n = len(df)
    base_wr = df["target"].mean()

    print(f"  Baseline: {n} trades, WR {base_wr*100:.1f}%")
    print(f"\n  From prior research (2026-04-18):")
    print(f"  - Claude Vision scores 1-10, corr +0.208 with outcome")
    print(f"  - Score >= 7: 36 trades, 75% WR (on 100-trade test set)")
    print(f"  - Score >= 6: ~55% WR")
    print(f"  - Cost: $0.004/request, latency 3-4 sec")
    print(f"  - BUT: test was on 100 hand-picked trades (6mo, 8 symbols)")
    print(f"  - Conservative estimate: OOS WR decay ~30-50%")

    # Model scenarios
    print(f"\n  Scenario modeling (what-if Vision filtering):")
    print(f"  {'Scenario':45s} {'N':>5} {'WR':>6} {'Exp':>9} {'Ann':>8} {'Cost/mo':>8}")

    scenarios = [
        # (name, filter_rate, wr_boost, cost_per_trade)
        ("No Vision (current best, ML thr=0.50)", 1.0, 0.0, 0),
        ("Vision optimistic: 40% pass, WR +15pp", 0.40, 0.15, 0.004),
        ("Vision realistic: 40% pass, WR +8pp", 0.40, 0.08, 0.004),
        ("Vision pessimistic: 40% pass, WR +4pp", 0.40, 0.04, 0.004),
        ("Vision on ML-filtered only (26 t/mo)", 0.40, 0.08, 0.004),
    ]

    # Use ML-filtered baseline (thr=0.50 from walk-forward = ~26 t/mo, 31% WR)
    ml_tpm = 26
    ml_wr = 0.309
    avg_win = df[df["target"]==1]["pnl_pct"].mean()
    avg_loss = abs(df[df["target"]==0]["pnl_pct"].mean())

    for name, filt, wr_boost, cost in scenarios:
        if "ML-filtered" in name:
            tpm = ml_tpm * filt
            wr = ml_wr + wr_boost
        elif "No Vision" in name:
            tpm = ml_tpm
            wr = ml_wr
            filt = 1.0
        else:
            base_tpm = n / period_months
            tpm = base_tpm * filt
            wr = base_wr + wr_boost

        exp = wr * avg_win - (1 - wr) * avg_loss
        monthly = tpm * exp * risk_pct / 100
        annual = (1 + monthly) ** 12 - 1
        monthly_cost = tpm / filt * cost * 30 if cost > 0 else 0  # score ALL, trade filtered

        print(f"  {name:45s} {tpm:>4.0f} {wr*100:>5.1f}% {exp*100:>+8.3f}% {annual*100:>+7.1f}% ${monthly_cost:>6.1f}")

    # Break-even analysis
    print(f"\n  Break-even: Vision needs to boost WR by how much?")
    for filt_rate in [0.3, 0.4, 0.5]:
        # Find WR that gives same annual as current ML (3.1%)
        target_annual = 0.031
        target_monthly = (1 + target_annual) ** (1/12) - 1
        tpm_v = ml_tpm * filt_rate
        # monthly = tpm * exp * risk / 100
        # target_monthly = tpm_v * (wr * avg_win - (1-wr)*avg_loss) * risk / 100
        # Solve for wr
        # Let a = avg_win + avg_loss, b = avg_loss
        # exp = wr * a - b
        # target_monthly * 100 / (tpm_v * risk) = wr * a - b
        a = avg_win + avg_loss
        b = avg_loss
        needed_exp = target_monthly * 100 / (tpm_v * risk_pct) if tpm_v > 0 else 999
        needed_wr = (needed_exp + b) / a if a > 0 else 999
        boost = needed_wr - ml_wr
        print(f"    Filter {filt_rate*100:.0f}% pass: need WR {needed_wr*100:.1f}% (+{boost*100:.1f}pp) to match 3.1% annual")


def main():
    print("=== Coin Selection + Vision Impact Analysis ===\n")

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    print(f"D1: {len(d1_data)}, 4H: {len(data_4h)} symbols")

    params = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150,
    )

    print("\nCollecting trades with coin features...")
    trades = collect_trades_with_coin_features(data_4h, d1_data, params)
    print(f"Total: {len(trades)} trades")

    analyze_coin_selection(trades)
    estimate_vision_impact(trades)


if __name__ == "__main__":
    main()
