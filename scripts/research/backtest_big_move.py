"""
Backtest: Big Move Detector standalone strategy.

Approach:
  - Walk-forward: train on 3 months, predict next 1 month, roll forward
  - Entry: when P(big_move) > threshold (0.5, 0.6, 0.7, 0.8)
  - Direction: RSI > 55 → long, RSI < 45 → short, else skip
  - TP/SL: based on predicted move size (5%+), with configurable R:R
  - Compare against: buy-and-hold BTC, Miro d1_only baseline

Uses features from reverse_pattern_discovery.py.

Usage:
    python scripts/research/backtest_big_move.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

DATA = ROOT / "data" / "processed" / "candles"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
    "AAVE/USDT", "UNI/USDT", "INJ/USDT", "TIA/USDT",
]

FEE_BPS = 10  # 0.1% per side


# ======================================================================
# Feature extraction (from reverse_pattern_discovery.py)
# ======================================================================

def extract_features(df: pd.DataFrame, idx: int, lookback: int = 24) -> dict:
    """Extract pre-move features at candle idx."""
    if idx < lookback + 10:
        return {}

    f = {}
    window = df.iloc[idx - lookback : idx]
    close = df["close"].iloc[idx]

    # Price action
    f["return_24h"] = (close / df["close"].iloc[idx - lookback] - 1) * 100
    f["return_12h"] = (close / df["close"].iloc[idx - lookback // 2] - 1) * 100
    f["return_6h"] = (close / df["close"].iloc[idx - lookback // 4] - 1) * 100
    f["abs_return_24h"] = abs(f["return_24h"])
    f["abs_return_12h"] = abs(f["return_12h"])

    # Volatility
    returns = window["close"].pct_change().dropna()
    f["volatility"] = float(returns.std() * 100) if len(returns) > 1 else 0

    f["range_pct"] = (window["high"].max() - window["low"].min()) / close * 100

    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - window["close"].shift(1)).abs(),
        (window["low"] - window["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    f["atr_pct"] = float(tr.mean() / close * 100)

    # Volume
    avg_vol = df["volume"].iloc[max(0, idx - lookback * 3) : idx - lookback].mean()
    recent_vol = window["volume"].mean()
    f["volume_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0
    f["volume_spike"] = float(window["volume"].max() / avg_vol) if avg_vol > 0 else 1.0

    split = lookback // 4
    vol_recent = window["volume"].iloc[-split:].mean()
    vol_earlier = window["volume"].iloc[:-split].mean()
    f["volume_trend"] = float(vol_recent / vol_earlier) if vol_earlier > 0 else 1.0

    # Candle patterns
    greens = (window["close"] > window["open"]).sum()
    f["green_ratio"] = greens / len(window)

    bodies = (window["close"] - window["open"]).abs()
    wicks_upper = window["high"] - window[["close", "open"]].max(axis=1)
    wicks_lower = window[["close", "open"]].min(axis=1) - window["low"]
    f["avg_body_pct"] = float((bodies / close).mean() * 100)
    f["avg_upper_wick_pct"] = float((wicks_upper / close).mean() * 100)
    f["avg_lower_wick_pct"] = float((wicks_lower / close).mean() * 100)

    last3 = df.iloc[idx - 3 : idx]
    f["last3_return"] = (df["close"].iloc[idx - 1] / df["close"].iloc[idx - 4] - 1) * 100
    f["last3_vol_ratio"] = float(last3["volume"].mean() / avg_vol) if avg_vol > 0 else 1.0

    # Trend
    sma_20 = df["close"].iloc[max(0, idx - 20) : idx].mean()
    sma_50 = df["close"].iloc[max(0, idx - 50) : idx].mean()
    f["dist_sma20_pct"] = (close - sma_20) / sma_20 * 100
    f["dist_sma50_pct"] = (close - sma_50) / sma_50 * 100

    if idx >= 20:
        f["sma20_slope"] = (sma_20 - df["close"].iloc[max(0, idx - 25) : max(1, idx - 5)].mean()) / close * 100
    else:
        f["sma20_slope"] = 0

    # RSI
    if idx >= 14:
        deltas = df["close"].iloc[idx - 14 : idx + 1].diff().dropna()
        gains = deltas.clip(lower=0).mean()
        losses_v = (-deltas.clip(upper=0)).mean()
        rs = gains / losses_v if losses_v > 0 else 100
        f["rsi"] = 100 - (100 / (1 + rs))
    else:
        f["rsi"] = 50

    # Volatility contraction (squeeze)
    if idx >= 40:
        std_20 = df["close"].iloc[idx - 20 : idx].std()
        std_prev = df["close"].iloc[idx - 40 : idx - 20].std()
        f["volatility_contraction"] = float(std_20 / std_prev) if std_prev > 0 else 1.0
    else:
        f["volatility_contraction"] = 1.0

    # Range bound detection
    range_high = window["high"].max()
    range_low = window["low"].min()
    near_high = (window["high"] > range_high * 0.995).sum()
    near_low = (window["low"] < range_low * 1.005).sum()
    f["touches_high"] = near_high
    f["touches_low"] = near_low
    f["is_range_bound"] = 1.0 if near_high >= 2 and near_low >= 2 else 0.0

    # Time
    ts = df["ts"].iloc[idx]
    if hasattr(ts, "hour"):
        f["hour_utc"] = float(ts.hour)
        f["day_of_week"] = float(ts.dayofweek)
    else:
        f["hour_utc"] = 12.0
        f["day_of_week"] = 3.0

    return f


# ======================================================================
# Label generation
# ======================================================================

def label_big_moves(df: pd.DataFrame, symbol: str,
                    min_move_pct: float = 5.0,
                    window: int = 6,
                    cooldown: int = 12) -> pd.Series:
    """Create binary labels: 1 if big move follows within `window` candles."""
    labels = pd.Series(0, index=df.index)

    last_move_idx = -cooldown
    for i in range(window, len(df) - window):
        if i - last_move_idx < cooldown:
            continue

        entry = df["close"].iloc[i]
        future_high = df["high"].iloc[i + 1 : i + 1 + window].max()
        future_low = df["low"].iloc[i + 1 : i + 1 + window].min()
        up = (future_high - entry) / entry * 100
        down = (entry - future_low) / entry * 100

        if up >= min_move_pct or down >= min_move_pct:
            labels.iloc[i] = 1
            last_move_idx = i

    return labels


def get_future_direction(df: pd.DataFrame, idx: int, window: int = 6) -> str:
    """What actually happened: up or down move."""
    entry = df["close"].iloc[idx]
    future_high = df["high"].iloc[idx + 1 : idx + 1 + window].max()
    future_low = df["low"].iloc[idx + 1 : idx + 1 + window].min()
    up = (future_high - entry) / entry * 100
    down = (entry - future_low) / entry * 100
    return "up" if up > down else "down"


# ======================================================================
# Walk-forward backtest
# ======================================================================

def load_data() -> dict[str, pd.DataFrame]:
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_1h.parquet"
        if path.exists():
            datasets[symbol] = pd.read_parquet(path)
    return datasets


def walk_forward_backtest(
    datasets: dict[str, pd.DataFrame],
    threshold: float = 0.7,
    min_move_pct: float = 5.0,
    move_window: int = 6,
    rr_ratio: float = 2.0,
    sl_pct: float = 2.5,
    train_candles: int = 24 * 90,   # 90 days
    test_candles: int = 24 * 30,    # 30 days
    step_candles: int = 24 * 30,    # roll 30 days
    rsi_long: float = 55.0,
    rsi_short: float = 45.0,
    max_hold: int = 12,             # max hold in candles
    cooldown_candles: int = 6,      # min candles between entries per symbol
) -> pd.DataFrame:
    """Walk-forward backtest across all symbols.

    Train on `train_candles`, predict on `test_candles`, roll forward.
    """
    feature_cols = None
    all_trades = []

    # Find max length across datasets
    max_len = max(len(df) for df in datasets.values())

    # Walk-forward windows
    start = 0
    window_num = 0

    while start + train_candles + test_candles <= max_len:
        train_end = start + train_candles
        test_end = train_end + test_candles

        # -- Collect training data --
        train_X, train_y = [], []
        for symbol, df in datasets.items():
            if len(df) < train_end:
                continue

            labels = label_big_moves(
                df.iloc[:train_end], symbol,
                min_move_pct=min_move_pct, window=move_window,
                cooldown=move_window * 2,
            )

            # Sample: all positives + 2x negatives
            pos_idx = labels[labels == 1].index.tolist()
            neg_candidates = [
                i for i in labels[labels == 0].index.tolist()
                if i > 34 and all(abs(i - p) > 24 for p in pos_idx)
            ]
            np.random.seed(42 + window_num)
            n_neg = min(len(pos_idx) * 2, len(neg_candidates))
            neg_idx = list(np.random.choice(neg_candidates, n_neg, replace=False)) if n_neg > 0 else []

            for idx in pos_idx + neg_idx:
                feat = extract_features(df, idx)
                if feat:
                    if feature_cols is None:
                        feature_cols = sorted(feat.keys())
                    train_X.append([feat.get(c, 0) for c in feature_cols])
                    train_y.append(labels.iloc[idx])

        if len(train_X) < 50:
            start += step_candles
            window_num += 1
            continue

        train_X = np.array(train_X)
        train_y = np.array(train_y)

        # -- Train model --
        clf = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            min_samples_leaf=max(5, len(train_X) // 50),
            subsample=0.8, random_state=42,
        )
        clf.fit(train_X, train_y)

        # -- Predict on test period --
        for symbol, df in datasets.items():
            t_start = max(train_end, 34)
            t_end = min(test_end, len(df) - move_window)
            if t_start >= t_end:
                continue

            last_entry_idx = -cooldown_candles
            active_trade = None

            for i in range(t_start, t_end):
                # Check active trade first
                if active_trade is not None:
                    h = df["high"].iloc[i]
                    l = df["low"].iloc[i]
                    at = active_trade

                    outcome, exit_price = None, None
                    if at["is_long"]:
                        if l <= at["sl"]:
                            outcome, exit_price = "sl_hit", at["sl"]
                        elif h >= at["tp"]:
                            outcome, exit_price = "tp_hit", at["tp"]
                        elif i - at["entry_idx"] > max_hold:
                            outcome, exit_price = "timeout", df["close"].iloc[i]
                    else:
                        if h >= at["sl"]:
                            outcome, exit_price = "sl_hit", at["sl"]
                        elif l <= at["tp"]:
                            outcome, exit_price = "tp_hit", at["tp"]
                        elif i - at["entry_idx"] > max_hold:
                            outcome, exit_price = "timeout", df["close"].iloc[i]

                    if outcome:
                        ep = at["entry_price"]
                        if at["is_long"]:
                            pnl_pct = (exit_price - ep) / ep * 100
                        else:
                            pnl_pct = (ep - exit_price) / ep * 100
                        pnl_pct -= 2 * FEE_BPS / 100  # fees both sides

                        all_trades.append({
                            "symbol": symbol,
                            "entry_time": df["ts"].iloc[at["entry_idx"]],
                            "exit_time": df["ts"].iloc[i],
                            "direction": "long" if at["is_long"] else "short",
                            "entry_price": ep,
                            "exit_price": exit_price,
                            "outcome": outcome,
                            "pnl_pct": pnl_pct,
                            "ml_prob": at["ml_prob"],
                            "rsi": at["rsi"],
                            "hold_candles": i - at["entry_idx"],
                            "window": window_num,
                        })
                        active_trade = None
                    else:
                        continue  # still in trade, skip new signals

                # Check for new signal
                if i - last_entry_idx < cooldown_candles:
                    continue

                feat = extract_features(df, i)
                if not feat:
                    continue

                x = np.array([[feat.get(c, 0) for c in feature_cols]])
                prob = clf.predict_proba(x)[0][1]

                if prob < threshold:
                    continue

                # Direction from RSI
                rsi = feat.get("rsi", 50)
                if rsi > rsi_long:
                    is_long = True
                elif rsi < rsi_short:
                    is_long = False
                else:
                    continue  # RSI neutral → skip

                entry_price = df["close"].iloc[i]
                if is_long:
                    sl = entry_price * (1 - sl_pct / 100)
                    tp = entry_price * (1 + sl_pct * rr_ratio / 100)
                else:
                    sl = entry_price * (1 + sl_pct / 100)
                    tp = entry_price * (1 - sl_pct * rr_ratio / 100)

                active_trade = {
                    "entry_idx": i,
                    "entry_price": entry_price,
                    "sl": sl, "tp": tp,
                    "is_long": is_long,
                    "ml_prob": prob,
                    "rsi": rsi,
                }
                last_entry_idx = i

        start += step_candles
        window_num += 1

    return pd.DataFrame(all_trades), feature_cols, clf


# ======================================================================
# Analysis
# ======================================================================

def analyze_results(trades_df: pd.DataFrame, name: str, period_months: float):
    """Print detailed stats for a backtest run."""
    if trades_df.empty:
        print(f"\n  {name}: NO TRADES")
        return {}

    df = trades_df.copy()
    n = len(df)
    tpm = n / period_months

    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    wr = len(wins) / n if n > 0 else 0

    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0
    total_pnl = df["pnl_pct"].sum()

    gross_win = wins["pnl_pct"].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses["pnl_pct"].sum()) if len(losses) > 0 else 1
    pf = gross_win / gross_loss if gross_loss > 0 else 0

    # Per-trade expectancy with 3% risk
    risk_pct = 3.0
    exp = wr * avg_win - (1 - wr) * abs(avg_loss)
    monthly = tpm * exp * risk_pct / 100
    annual = (1 + monthly) ** 12 - 1

    # Equity curve (cumulative %, 3% risk per trade)
    pnl_series = df["pnl_pct"].values * risk_pct / 100
    equity = np.cumprod(1 + pnl_series / 100)
    max_dd = 0
    peak = equity[0]
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    avg_hold = df["hold_candles"].mean()

    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    print(f"  Trades: {n} ({tpm:.1f}/mo)")
    print(f"  WR: {wr*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg win: +{avg_win:.2f}%  Avg loss: {avg_loss:.2f}%")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Expectancy: {exp:+.3f}% per trade")
    print(f"  Monthly (3% risk): {monthly*100:+.2f}%")
    print(f"  Annual: {annual*100:+.1f}%")
    print(f"  Max drawdown: {max_dd*100:.1f}%")
    print(f"  Avg hold: {avg_hold:.1f}h")

    # By direction
    for d in ["long", "short"]:
        sub = df[df["direction"] == d]
        if len(sub) > 0:
            sw = sub[sub["pnl_pct"] > 0]
            print(f"  {d.upper():>5}: {len(sub)}t, WR {len(sw)/len(sub)*100:.0f}%, "
                  f"avg {sub['pnl_pct'].mean():+.2f}%")

    # By outcome
    for o in ["tp_hit", "sl_hit", "timeout"]:
        sub = df[df["outcome"] == o]
        if len(sub) > 0:
            print(f"  {o:>8}: {len(sub)} ({len(sub)/n*100:.0f}%), avg {sub['pnl_pct'].mean():+.2f}%")

    # By symbol (top/bottom)
    sym_stats = []
    for sym in df["symbol"].unique():
        sub = df[df["symbol"] == sym]
        if len(sub) >= 3:
            sym_stats.append((sym, len(sub), sub["pnl_pct"].sum(), sub[sub["pnl_pct"]>0].shape[0]/len(sub)))
    sym_stats.sort(key=lambda x: x[2], reverse=True)
    if sym_stats:
        print(f"\n  Per-symbol (top 5):")
        for sym, cnt, pnl, swr in sym_stats[:5]:
            print(f"    {sym:12s}: {cnt:3d}t, WR {swr*100:.0f}%, total {pnl:+.2f}%")
        if len(sym_stats) > 5:
            print(f"  Bottom 3:")
            for sym, cnt, pnl, swr in sym_stats[-3:]:
                print(f"    {sym:12s}: {cnt:3d}t, WR {swr*100:.0f}%, total {pnl:+.2f}%")

    return {
        "name": name, "trades": n, "tpm": tpm, "wr": wr, "pf": pf,
        "exp": exp, "monthly": monthly, "annual": annual, "max_dd": max_dd,
    }


def main():
    print("=== Big Move Detector — Standalone Backtest ===\n")

    datasets = load_data()
    print(f"Loaded {len(datasets)} symbols (1h data)")

    if not datasets:
        print("No data! Run reverse_pattern_discovery.py first to download.")
        return

    # Total period in months
    lengths = [len(df) for df in datasets.values()]
    avg_candles = sum(lengths) / len(lengths)
    period_months = avg_candles / (24 * 30)
    # Test period is total minus first train window (3 months)
    test_months = max(1, period_months - 3)

    print(f"Avg data: {avg_candles:.0f} candles ({period_months:.1f} months)")
    print(f"Test period: ~{test_months:.1f} months (walk-forward)\n")

    configs = [
        # (name, threshold, sl_pct, rr, max_hold, rsi_long, rsi_short)
        ("thr=0.5 RR2 SL2.5%", 0.5, 2.5, 2.0, 12, 55, 45),
        ("thr=0.6 RR2 SL2.5%", 0.6, 2.5, 2.0, 12, 55, 45),
        ("thr=0.7 RR2 SL2.5%", 0.7, 2.5, 2.0, 12, 55, 45),
        ("thr=0.8 RR2 SL2.5%", 0.8, 2.5, 2.0, 12, 55, 45),

        # Tighter SL, higher RR
        ("thr=0.7 RR3 SL2%",   0.7, 2.0, 3.0, 12, 55, 45),

        # Wider RSI filter
        ("thr=0.7 RSI60/40",   0.7, 2.5, 2.0, 12, 60, 40),

        # Longer hold
        ("thr=0.7 hold=24",    0.7, 2.5, 2.0, 24, 55, 45),
    ]

    summaries = []

    for name, thr, sl, rr, mh, rsi_l, rsi_s in configs:
        print(f"\nRunning: {name}...")

        trades_df, feat_cols, clf = walk_forward_backtest(
            datasets,
            threshold=thr,
            sl_pct=sl,
            rr_ratio=rr,
            max_hold=mh,
            rsi_long=rsi_l,
            rsi_short=rsi_s,
        )

        result = analyze_results(trades_df, name, test_months)
        if result:
            summaries.append(result)

    # Final comparison
    if summaries:
        print(f"\n{'='*70}")
        print(f"  COMPARISON TABLE")
        print(f"{'='*70}")
        print(f"  {'Config':<25} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Annual':>8} {'MaxDD':>6}")
        for s in sorted(summaries, key=lambda x: x["annual"], reverse=True):
            print(f"  {s['name']:<25} {s['trades']:>5} {s['tpm']:>4.1f} "
                  f"{s['wr']*100:>5.1f}% {s['pf']:>5.2f} "
                  f"{s['exp']:>+8.3f}% {s['annual']*100:>+7.1f}% {s['max_dd']*100:>5.1f}%")

        best = max(summaries, key=lambda x: x["annual"])
        print(f"\n  Best: {best['name']} -> {best['annual']*100:+.1f}% annual")

    # Feature importance from last model
    if feat_cols and clf:
        fi = sorted(zip(feat_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
        print(f"\n  Feature importance (last window):")
        for name, imp in fi[:10]:
            bar = "#" * int(imp * 100)
            print(f"    {name:<25} {imp:.4f} {bar}")


if __name__ == "__main__":
    main()
