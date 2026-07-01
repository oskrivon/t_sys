"""Range Trading Backtest v2 — corridor-based: buy support, sell resistance.

Logic:
  1. Detect rolling S/R levels (walk-forward, no look-ahead)
  2. Identify corridor: nearest support below + nearest resistance above
  3. Price touches support -> LONG, TP = resistance
  4. Price touches resistance -> SHORT, TP = support
  5. SL: beyond the level zone edge
  6. Natural R:R from corridor width vs SL distance
  7. Filters: min corridor width, min level score, cooldown, RSI confirmation

Split: first 50% = TRAIN, second 50% = TEST.
Then: ML filter trained on TRAIN, validated on TEST.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy.levels import get_rolling_levels
from src.strategy.models import Level
from src.backtest.runner import CandleStrategy
from src.backtest.presets import bybit_futures
from src.backtest.models import Trade, ExitReason, Side
from src.backtest.metrics import compute_metrics


# -- Config -------------------------------------------------------------------
TIMEFRAME = "4h"
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ARBUSDT", "SUIUSDT",
]
DATA_DIR = Path("data/processed/candles")

# Level detection
LOOKBACK = 200
MIN_TOUCHES = 2
TOLERANCE_PCT = 1.0
MIN_LEVEL_AGE = 10
SWING_ORDER = 5

# Entry logic
SL_BUFFER_PCT = 0.3       # SL beyond zone edge
MIN_CORRIDOR_PCT = 2.0    # min corridor width in % (skip narrow ranges)
MIN_RR = 1.5              # min reward:risk ratio
MAX_HOLD = 60             # max hold candles (~10 days on 4h)
COOLDOWN = 6              # min candles between signals per symbol

# ML later — collect all signals with features for training
COLLECT_FEATURES = True


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def compute_bbands(closes: pd.Series, period: int = 20, std: float = 2.0):
    sma = closes.rolling(period).mean()
    std_dev = closes.rolling(period).std()
    return sma, sma + std * std_dev, sma - std * std_dev


class RangeStrategy(CandleStrategy):
    """Corridor-based range trading: buy support, sell resistance."""

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> list[dict]:
        signals = []
        cached_levels = []
        last_level_calc = -999
        last_signal_idx = -999

        rsi = compute_rsi(df["close"])
        sma20 = df["close"].rolling(20).mean()
        sma50 = df["close"].rolling(50).mean()
        atr = (df["high"] - df["low"]).rolling(20).mean()
        vol_ma = df["volume"].rolling(20).mean()

        for idx in range(LOOKBACK + SWING_ORDER, len(df) - 1):
            if idx - last_signal_idx < COOLDOWN:
                continue

            # Recalculate levels every 6 candles (24h)
            if idx - last_level_calc >= 6:
                cached_levels = get_rolling_levels(
                    df, idx,
                    lookback=LOOKBACK,
                    min_touches=MIN_TOUCHES,
                    tolerance_pct=TOLERANCE_PCT,
                    min_level_age=MIN_LEVEL_AGE,
                    swing_order=SWING_ORDER,
                )
                last_level_calc = idx

            if len(cached_levels) < 2:
                continue

            close = df["close"].iloc[idx]
            low = df["low"].iloc[idx]
            high = df["high"].iloc[idx]
            cur_rsi = rsi.iloc[idx] if idx < len(rsi) else 50.0
            cur_atr = atr.iloc[idx] if idx < len(atr) else 0.0

            # Sort levels by price
            levels_sorted = sorted(cached_levels, key=lambda lv: lv.price)
            supports = [lv for lv in levels_sorted if lv.price < close]
            resistances = [lv for lv in levels_sorted if lv.price > close]

            if not supports or not resistances:
                continue

            sup = supports[-1]   # nearest support
            res = resistances[0]  # nearest resistance

            # Corridor width
            corridor_pct = (res.price - sup.price) / sup.price * 100
            if corridor_pct < MIN_CORRIDOR_PCT:
                continue

            # Features for ML
            features = {}
            if COLLECT_FEATURES:
                features = {
                    "corridor_pct": round(corridor_pct, 2),
                    "sup_touches": sup.touches,
                    "sup_score": sup.score,
                    "res_touches": res.touches,
                    "res_score": res.score,
                    "rsi": round(cur_rsi, 1),
                    "atr_pct": round(cur_atr / close * 100, 3),
                    "vol_ratio": round(df["volume"].iloc[idx] / vol_ma.iloc[idx], 2)
                        if vol_ma.iloc[idx] > 0 else 1.0,
                    "dist_to_sup_pct": round((close - sup.price) / close * 100, 2),
                    "dist_to_res_pct": round((res.price - close) / close * 100, 2),
                    "sma20_dist": round((close - sma20.iloc[idx]) / close * 100, 2)
                        if pd.notna(sma20.iloc[idx]) else 0.0,
                    "sma50_dist": round((close - sma50.iloc[idx]) / close * 100, 2)
                        if pd.notna(sma50.iloc[idx]) else 0.0,
                    "price_change_7d": round(
                        (close - df["close"].iloc[max(0, idx-42)]) /
                        df["close"].iloc[max(0, idx-42)] * 100, 2),
                    "combined_score": sup.score + res.score,
                    "combined_touches": sup.touches + res.touches,
                }

            # -- LONG at support --
            sup_zone_w = sup.zone_high - sup.zone_low
            touch_sup = sup.zone_high + sup_zone_w * 0.5  # price within half zone-width

            if low <= touch_sup and close > sup.zone_low:
                sl = sup.zone_low * (1 - SL_BUFFER_PCT / 100)
                tp = res.zone_low  # TP = bottom of resistance zone
                risk = close - sl
                reward = tp - close

                if risk > 0 and reward / risk >= MIN_RR:
                    sig = {
                        "entry_idx": idx,
                        "entry_price": close,
                        "side": "long",
                        "sl": sl,
                        "tp": tp,
                        "metadata": {**features, "type": "range_long",
                                     "rr": round(reward / risk, 2)},
                    }
                    signals.append(sig)
                    last_signal_idx = idx
                    continue

            # -- SHORT at resistance --
            res_zone_w = res.zone_high - res.zone_low
            touch_res = res.zone_low - res_zone_w * 0.5

            if high >= touch_res and close < res.zone_high:
                sl = res.zone_high * (1 + SL_BUFFER_PCT / 100)
                tp = sup.zone_high  # TP = top of support zone
                risk = sl - close
                reward = close - tp

                if risk > 0 and reward / risk >= MIN_RR:
                    sig = {
                        "entry_idx": idx,
                        "entry_price": close,
                        "side": "short",
                        "sl": sl,
                        "tp": tp,
                        "metadata": {**features, "type": "range_short",
                                     "rr": round(reward / risk, 2)},
                    }
                    signals.append(sig)
                    last_signal_idx = idx

        return signals


# -- Data helpers -------------------------------------------------------------

def load_data(symbols: list[str], timeframe: str) -> dict[str, pd.DataFrame]:
    data = {}
    for sym in symbols:
        path = DATA_DIR / f"{sym}_{timeframe}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if len(df) < LOOKBACK + 100:
            continue
        data[sym] = df
    return data


def split_data(data: dict[str, pd.DataFrame], frac: float = 0.5):
    train, test = {}, {}
    for sym, df in data.items():
        mid = int(len(df) * frac)
        train[sym] = df.iloc[:mid].reset_index(drop=True)
        test[sym] = df.iloc[mid:].reset_index(drop=True)
    return train, test


def print_metrics(label: str, trades: list[Trade]):
    if not trades:
        print(f"\n{'='*60}")
        print(f"  {label}: 0 trades")
        print(f"{'='*60}")
        return None

    m = compute_metrics(trades)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trades:        {m.n_trades}")
    print(f"  Win rate:      {m.win_rate:.1%}")
    print(f"  Profit factor: {m.profit_factor:.2f}")
    print(f"  Expectancy:    {m.expectancy_pct:+.3f}%")
    print(f"  Sharpe:        {m.sharpe:.2f}")
    print(f"  Max DD:        {m.max_drawdown_pct:.1f}%")
    print(f"  Annual return: {m.annual_return_pct:+.1f}%")
    print(f"  Trades/month:  {m.trades_per_month:.1f}")
    print(f"  Avg hold (h):  {m.avg_hold_hours:.0f}")
    print(f"  Avg win:       {m.avg_win_pct:+.3f}%")
    print(f"  Avg loss:      {m.avg_loss_pct:+.3f}%")

    reasons = {}
    for t in trades:
        r = t.exit_reason.value
        reasons[r] = reasons.get(r, 0) + 1
    print(f"  Exit reasons:  {reasons}")

    longs = [t for t in trades if t.side.value == "long"]
    shorts = [t for t in trades if t.side.value == "short"]
    if longs:
        lm = compute_metrics(longs)
        print(f"  LONG:  {len(longs)} trades, WR {lm.win_rate:.1%}, "
              f"exp {lm.expectancy_pct:+.3f}%, PF {lm.profit_factor:.2f}")
    if shorts:
        sm = compute_metrics(shorts)
        print(f"  SHORT: {len(shorts)} trades, WR {sm.win_rate:.1%}, "
              f"exp {sm.expectancy_pct:+.3f}%, PF {sm.profit_factor:.2f}")

    return m


def analyze_features(trades: list[Trade], label: str):
    """Analyze which features correlate with wins."""
    if not trades:
        return

    rows = []
    for t in trades:
        meta = t.metadata or {}
        win = 1 if t.net_pnl_pct > 0 else 0
        row = {**meta, "win": win, "net_pnl_pct": t.net_pnl_pct}
        rows.append(row)

    df = pd.DataFrame(rows)
    numeric = df.select_dtypes(include=[np.number])
    if "win" not in numeric.columns or len(numeric) < 20:
        return

    print(f"\n  Feature correlations with win ({label}):")
    corrs = numeric.corr()["win"].drop(["win", "net_pnl_pct"], errors="ignore")
    corrs = corrs.dropna().sort_values(key=abs, ascending=False)
    for feat, corr in corrs.head(10).items():
        print(f"    {feat:20s}  r={corr:+.3f}")


def run_ml_filter(train_trades: list[Trade], test_trades: list[Trade]):
    """Train ML on TRAIN signals, apply to TEST."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import classification_report

    feature_cols = [
        "corridor_pct", "sup_touches", "sup_score", "res_touches", "res_score",
        "rsi", "atr_pct", "vol_ratio", "dist_to_sup_pct", "dist_to_res_pct",
        "sma20_dist", "sma50_dist", "price_change_7d", "combined_score",
        "combined_touches", "rr",
    ]

    def trades_to_xy(trades):
        X_rows, y_rows = [], []
        for t in trades:
            meta = t.metadata or {}
            row = [meta.get(f, 0) for f in feature_cols]
            if any(pd.isna(v) for v in row):
                continue
            X_rows.append(row)
            y_rows.append(1 if t.net_pnl_pct > 0 else 0)
        return np.array(X_rows), np.array(y_rows)

    X_train, y_train = trades_to_xy(train_trades)
    X_test, y_test = trades_to_xy(test_trades)

    if len(X_train) < 30 or len(X_test) < 10:
        print(f"\n  ML: not enough data (train={len(X_train)}, test={len(X_test)})")
        return

    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, min_samples_leaf=5,
        learning_rate=0.1, random_state=42,
    )
    clf.fit(X_train, y_train)

    # Feature importance
    print(f"\n{'='*60}")
    print(f"  ML Feature Importance (top 10)")
    print(f"{'='*60}")
    imp = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: -x[1])
    for feat, importance in imp[:10]:
        print(f"    {feat:20s}  {importance:.3f}")

    # Train accuracy
    train_pred = clf.predict(X_train)
    train_proba = clf.predict_proba(X_train)[:, 1]
    print(f"\n  TRAIN: accuracy={np.mean(train_pred == y_train):.1%}, "
          f"base WR={y_train.mean():.1%}")

    # Test predictions
    test_proba = clf.predict_proba(X_test)[:, 1]

    print(f"\n{'='*60}")
    print(f"  ML Filter Results (TEST set)")
    print(f"{'='*60}")

    for threshold in [0.3, 0.4, 0.5, 0.6]:
        mask = test_proba >= threshold
        n_pass = mask.sum()
        if n_pass == 0:
            print(f"  P>={threshold}: 0 trades pass")
            continue
        wr = y_test[mask].mean()
        # Reconstruct filtered trades for PnL
        filtered = [t for t, m in zip(test_trades, mask) if m]
        if filtered:
            fm = compute_metrics(filtered)
            print(f"  P>={threshold}: {n_pass:3d} trades, WR {wr:.1%}, "
                  f"exp {fm.expectancy_pct:+.3f}%, PF {fm.profit_factor:.2f}, "
                  f"annual {fm.annual_return_pct:+.1f}%")


# -- Main ---------------------------------------------------------------------

def main():
    print("Loading data...")
    data = load_data(SYMBOLS, TIMEFRAME)
    print(f"Loaded {len(data)} symbols, {TIMEFRAME}")
    for sym, df in data.items():
        ts = df["ts"]
        print(f"  {sym:12s}: {len(df):5d} candles  "
              f"{ts.iloc[0].strftime('%Y-%m-%d')} -> {ts.iloc[-1].strftime('%Y-%m-%d')}")

    train_data, test_data = split_data(data, frac=0.5)
    print(f"\nSplit: TRAIN first 50%, TEST second 50%")

    strategy = RangeStrategy()
    cost = bybit_futures()

    # -- TRAIN --
    print("\nRunning TRAIN...")
    train_trades = strategy.run(train_data, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(train_trades)
    print_metrics("TRAIN (first 50%)", train_trades)
    analyze_features(train_trades, "TRAIN")

    # -- TEST --
    print("\nRunning TEST...")
    test_trades = strategy.run(test_data, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(test_trades)
    print_metrics("TEST (second 50%)", test_trades)
    analyze_features(test_trades, "TEST")

    # -- ML Filter --
    if len(train_trades) >= 30 and len(test_trades) >= 10:
        run_ml_filter(train_trades, test_trades)

    print("\nDone.")


if __name__ == "__main__":
    main()
