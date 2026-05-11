"""Range Trading: Trail 0.5% + ML + Vision filter combined test."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research.range_rsi_filter import RangeRSIStrategy, LOOKBACK, MAX_HOLD
from src.backtest.presets import bybit_futures
from src.backtest.metrics import compute_metrics
from sklearn.ensemble import GradientBoostingClassifier

DATA_DIR = Path("data/processed/candles")
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ARBUSDT", "SUIUSDT",
]

FEATURE_COLS = [
    "corridor_pct", "sup_touches", "sup_score", "res_touches", "res_score",
    "rsi", "atr_pct", "vol_ratio", "dist_to_sup_pct", "dist_to_res_pct",
    "sma20_dist", "sma50_dist", "price_change_7d", "combined_score",
    "combined_touches", "rr",
]


class RangeTrailingStrategy(RangeRSIStrategy):
    def __init__(self, trail_pct=0.5):
        super().__init__()
        self.trail_pct = trail_pct

    def simulate_exit(self, df, signal, max_hold=200):
        entry_idx = signal["entry_idx"]
        entry_price = signal["entry_price"]
        is_long = signal["side"] == "long"
        sl = signal["sl"]
        tp = signal["tp"]
        best_price = entry_price

        for i in range(entry_idx + 1, min(entry_idx + max_hold + 1, len(df))):
            h = df["high"].iloc[i]
            lo = df["low"].iloc[i]

            if is_long:
                if h > best_price:
                    best_price = h
                trail_sl = best_price * (1 - self.trail_pct / 100)
                sl = max(sl, trail_sl)
                if lo <= sl:
                    return {"exit_idx": i, "exit_price": sl, "reason": "sl"}
                if h >= tp:
                    return {"exit_idx": i, "exit_price": tp, "reason": "tp"}
            else:
                if lo < best_price:
                    best_price = lo
                trail_sl = best_price * (1 + self.trail_pct / 100)
                sl = min(sl, trail_sl)
                if h >= sl:
                    return {"exit_idx": i, "exit_price": sl, "reason": "sl"}
                if lo <= tp:
                    return {"exit_idx": i, "exit_price": tp, "reason": "tp"}

        exit_idx = min(entry_idx + max_hold, len(df) - 1)
        return {
            "exit_idx": exit_idx,
            "exit_price": df["close"].iloc[exit_idx],
            "reason": "timeout",
        }


def to_xy(trades):
    X, y = [], []
    for t in trades:
        meta = t.metadata or {}
        row = [meta.get(f, 0) for f in FEATURE_COLS]
        if any(pd.isna(v) for v in row):
            continue
        X.append(row)
        y.append(1 if t.net_pnl_pct > 0 else 0)
    return np.array(X), np.array(y)


def fmt(trades):
    if not trades or len(trades) < 3:
        return f"N={len(trades) if trades else 0} (too few)"
    m = compute_metrics(trades)
    return (f"N={m.n_trades:4d} WR={m.win_rate:.1%} PF={m.profit_factor:.2f} "
            f"Sh={m.sharpe:6.2f} Ann={m.annual_return_pct:+.1f}% "
            f"Exp={m.expectancy_pct:+.4f}% t/mo={m.trades_per_month:.1f}")


def main():
    # Load data
    data = {}
    for sym in SYMBOLS:
        path = DATA_DIR / f"{sym}_4h.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if len(df) < LOOKBACK + 100:
            continue
        data[sym] = df

    train_d, test_d = {}, {}
    for sym, df in data.items():
        mid = int(len(df) * 0.5)
        train_d[sym] = df.iloc[:mid].reset_index(drop=True)
        test_d[sym] = df.iloc[mid:].reset_index(drop=True)

    cost = bybit_futures()

    # Train ML on FIXED SL/TP train data (same model for both exit types)
    baseline = RangeRSIStrategy()
    train_trades_base = baseline.run(train_d, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(train_trades_base)

    X_train, y_train = to_xy(train_trades_base)
    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, min_samples_leaf=5,
        learning_rate=0.1, random_state=42,
    )
    clf.fit(X_train, y_train)

    # Test both exit types
    for label, strategy in [("Fixed SL/TP", RangeRSIStrategy()),
                            ("Trail 0.5%", RangeTrailingStrategy(0.5))]:
        test_trades = strategy.run(test_d, position_size=1000, max_hold=MAX_HOLD)
        cost.apply_all(test_trades)

        X_test, _ = to_xy(test_trades)
        proba = clf.predict_proba(X_test)[:, 1]

        print(f"\n=== {label} ===")
        print(f"  No filter:   {fmt(test_trades)}")

        for thr in [0.3, 0.4, 0.5]:
            mask = proba >= thr
            filtered = [t for t, m in zip(test_trades, mask) if m]
            print(f"  ML P>={thr}:    {fmt(filtered)}")

    # Vision filter on trailing
    print("\n=== Trail 0.5% + ML P>=0.3 + Vision ===")

    trail_strat = RangeTrailingStrategy(0.5)
    test_trail = trail_strat.run(test_d, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(test_trail)

    X_t, _ = to_xy(test_trail)
    proba_t = clf.predict_proba(X_t)[:, 1]
    ml_filtered = [(t, p) for t, p in zip(test_trail, proba_t) if p >= 0.3]

    # Load vision scores — match by symbol + side in order
    vision_df = pd.read_csv("data/reports/range_vision_full_results.csv")

    # Build vision score lookup: symbol+side -> ordered list of scores
    vision_map = {}
    for _, row in vision_df.iterrows():
        key = (row["symbol"], row["side"])
        vision_map.setdefault(key, []).append(row["score"])

    # Match ML-filtered trailing trades with vision scores
    trail_map = {}
    for t, p in ml_filtered:
        sym = t.symbol.replace("/", "").replace(":USDT", "")
        key = (sym, t.side.value)
        trail_map.setdefault(key, []).append(t)

    matched = []
    for key in trail_map:
        scores = vision_map.get(key, [])
        for i, t in enumerate(trail_map[key]):
            score = scores[i] if i < len(scores) else None
            matched.append((t, score))

    n_with_score = sum(1 for _, s in matched if s is not None)
    print(f"  Matched: {len(matched)} trades, {n_with_score} with vision score")

    for v_min in [0, 5, 6, 7]:
        if v_min == 0:
            filtered = [t for t, s in matched]
            lbl = "ML P>=0.3 only"
        else:
            filtered = [t for t, s in matched if s is not None and s >= v_min]
            lbl = f"ML + Vision>={v_min}"

        if len(filtered) < 3:
            print(f"  {lbl:25s} N={len(filtered)} (too few)")
            continue
        m = compute_metrics(filtered)
        longs = [t for t in filtered if t.side.value == "long"]
        shorts = [t for t in filtered if t.side.value == "short"]
        l_info = f" L:{len(longs)}t" if longs else ""
        s_info = f" S:{len(shorts)}t" if shorts else ""
        print(f"  {lbl:25s} {fmt(filtered)}{l_info}{s_info}")


if __name__ == "__main__":
    main()
