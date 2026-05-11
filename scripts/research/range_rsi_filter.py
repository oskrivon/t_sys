"""Range Trading + RSI confirmation filter — research.

Test: does RSI confirmation improve range trading?
- LONG at support only if RSI < threshold (oversold)
- SHORT at resistance only if RSI > (100 - threshold) (overbought)

Varies RSI thresholds: 30, 35, 40, 45, 50 (no filter = baseline).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy.levels import get_rolling_levels
from src.backtest.runner import CandleStrategy
from src.backtest.presets import bybit_futures
from src.backtest.models import Trade
from src.backtest.metrics import compute_metrics


# -- Config (same as backtest_range_trading.py) --------------------------------
TIMEFRAME = "4h"
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ARBUSDT", "SUIUSDT",
]
DATA_DIR = Path("data/processed/candles")

LOOKBACK = 200
MIN_TOUCHES = 2
TOLERANCE_PCT = 1.0
MIN_LEVEL_AGE = 10
SWING_ORDER = 5

SL_BUFFER_PCT = 0.3
MIN_CORRIDOR_PCT = 2.0
MIN_RR = 1.5
MAX_HOLD = 60
COOLDOWN = 6


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


class RangeRSIStrategy(CandleStrategy):
    """Range trading with configurable RSI filter."""

    def __init__(self, rsi_long_max: float = 100, rsi_short_min: float = 0):
        self.rsi_long_max = rsi_long_max    # long only if RSI < this
        self.rsi_short_min = rsi_short_min  # short only if RSI > this

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> list[dict]:
        signals = []
        cached_levels = []
        last_level_calc = -999
        last_signal_idx = -999

        rsi = compute_rsi(df["close"])

        for idx in range(LOOKBACK + SWING_ORDER, len(df) - 1):
            if idx - last_signal_idx < COOLDOWN:
                continue

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

            levels_sorted = sorted(cached_levels, key=lambda lv: lv.price)
            supports = [lv for lv in levels_sorted if lv.price < close]
            resistances = [lv for lv in levels_sorted if lv.price > close]

            if not supports or not resistances:
                continue

            sup = supports[-1]
            res = resistances[0]

            corridor_pct = (res.price - sup.price) / sup.price * 100
            if corridor_pct < MIN_CORRIDOR_PCT:
                continue

            # -- LONG at support + RSI filter --
            sup_zone_w = sup.zone_high - sup.zone_low
            touch_sup = sup.zone_high + sup_zone_w * 0.5

            if low <= touch_sup and close > sup.zone_low:
                if cur_rsi <= self.rsi_long_max:  # RSI filter
                    sl = sup.zone_low * (1 - SL_BUFFER_PCT / 100)
                    tp = res.zone_low
                    risk = close - sl
                    reward = tp - close

                    if risk > 0 and reward / risk >= MIN_RR:
                        signals.append({
                            "entry_idx": idx,
                            "entry_price": close,
                            "side": "long",
                            "sl": sl,
                            "tp": tp,
                            "metadata": {"rsi": round(cur_rsi, 1),
                                         "rr": round(reward / risk, 2)},
                        })
                        last_signal_idx = idx
                        continue

            # -- SHORT at resistance + RSI filter --
            res_zone_w = res.zone_high - res.zone_low
            touch_res = res.zone_low - res_zone_w * 0.5

            if high >= touch_res and close < res.zone_high:
                if cur_rsi >= self.rsi_short_min:  # RSI filter
                    sl = res.zone_high * (1 + SL_BUFFER_PCT / 100)
                    tp = sup.zone_high
                    risk = sl - close
                    reward = close - tp

                    if risk > 0 and reward / risk >= MIN_RR:
                        signals.append({
                            "entry_idx": idx,
                            "entry_price": close,
                            "side": "short",
                            "sl": sl,
                            "tp": tp,
                            "metadata": {"rsi": round(cur_rsi, 1),
                                         "rr": round(reward / risk, 2)},
                        })
                        last_signal_idx = idx

        return signals


def load_data():
    data = {}
    for sym in SYMBOLS:
        path = DATA_DIR / f"{sym}_{TIMEFRAME}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if len(df) < LOOKBACK + 100:
            continue
        data[sym] = df
    return data


def split_data(data, frac=0.5):
    train, test = {}, {}
    for sym, df in data.items():
        mid = int(len(df) * frac)
        train[sym] = df.iloc[:mid].reset_index(drop=True)
        test[sym] = df.iloc[mid:].reset_index(drop=True)
    return train, test


def main():
    print("Loading data...")
    data = load_data()
    print(f"Loaded {len(data)} symbols")
    train_data, test_data = split_data(data)

    cost = bybit_futures()

    # RSI filter configs: (long_max, short_min, label)
    configs = [
        (100, 0,  "No filter (baseline)"),
        (50,  50, "RSI < 50 / > 50"),
        (45,  55, "RSI < 45 / > 55"),
        (40,  60, "RSI < 40 / > 60"),
        (35,  65, "RSI < 35 / > 65"),
        (30,  70, "RSI < 30 / > 70"),
    ]

    print(f"\n{'Label':30s} | {'N':>5s} | {'WR':>6s} | {'PF':>6s} | {'Exp%':>8s} | "
          f"{'Sharpe':>6s} | {'Annual':>8s} | {'N':>5s} | {'WR':>6s} | {'PF':>6s} | "
          f"{'Exp%':>8s} | {'Sharpe':>6s} | {'Annual':>8s}")
    print(f"{'':30s} | {'---TRAIN---':^37s} | {'---TEST---':^37s}")
    print("-" * 120)

    for rsi_long, rsi_short, label in configs:
        strategy = RangeRSIStrategy(rsi_long_max=rsi_long, rsi_short_min=rsi_short)

        train_trades = strategy.run(train_data, position_size=1000, max_hold=MAX_HOLD)
        cost.apply_all(train_trades)

        test_trades = strategy.run(test_data, position_size=1000, max_hold=MAX_HOLD)
        cost.apply_all(test_trades)

        def fmt(trades):
            if not trades:
                return f"{'0':>5s} | {'--':>6s} | {'--':>6s} | {'--':>8s} | {'--':>6s} | {'--':>8s}"
            m = compute_metrics(trades)
            return (f"{m.n_trades:5d} | {m.win_rate:6.1%} | {m.profit_factor:6.2f} | "
                    f"{m.expectancy_pct:+8.3f} | {m.sharpe:6.2f} | {m.annual_return_pct:+8.1f}")

        print(f"{label:30s} | {fmt(train_trades)} | {fmt(test_trades)}")

        # Long/short breakdown for interesting configs
        if rsi_long <= 45:
            for side_label, side_val in [("  LONG", "long"), ("  SHORT", "short")]:
                train_side = [t for t in train_trades if t.side.value == side_val]
                test_side = [t for t in test_trades if t.side.value == side_val]
                print(f"{side_label + ' ' + label:30s} | {fmt(train_side)} | {fmt(test_side)}")

    # Detailed analysis: RSI distribution in wins vs losses (baseline)
    print(f"\n{'='*60}")
    print(f"  RSI distribution: wins vs losses (TEST, no filter)")
    print(f"{'='*60}")

    strategy = RangeRSIStrategy()
    test_trades = strategy.run(test_data, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(test_trades)

    longs = [t for t in test_trades if t.side.value == "long"]
    shorts = [t for t in test_trades if t.side.value == "short"]

    for label, trades in [("LONG", longs), ("SHORT", shorts)]:
        wins = [t for t in trades if t.net_pnl_pct > 0]
        losses = [t for t in trades if t.net_pnl_pct <= 0]
        win_rsi = [t.metadata.get("rsi", 50) for t in wins]
        loss_rsi = [t.metadata.get("rsi", 50) for t in losses]
        if win_rsi and loss_rsi:
            print(f"  {label} wins  (n={len(wins):3d}): RSI mean={np.mean(win_rsi):.1f}, "
                  f"median={np.median(win_rsi):.1f}, <40: {sum(1 for r in win_rsi if r<40)}, "
                  f"<30: {sum(1 for r in win_rsi if r<30)}")
            print(f"  {label} losses(n={len(losses):3d}): RSI mean={np.mean(loss_rsi):.1f}, "
                  f"median={np.median(loss_rsi):.1f}, <40: {sum(1 for r in loss_rsi if r<40)}, "
                  f"<30: {sum(1 for r in loss_rsi if r<30)}")

    for label, trades in [("LONG", longs), ("SHORT", shorts)]:
        print(f"\n  {label} WR by RSI bucket:")
        rsi_data = [(t.metadata.get("rsi", 50), t.net_pnl_pct > 0) for t in trades]
        for lo, hi in [(0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 100)]:
            bucket = [(r, w) for r, w in rsi_data if lo <= r < hi]
            if bucket:
                wr = sum(w for _, w in bucket) / len(bucket)
                print(f"    RSI {lo:2d}-{hi:2d}: {len(bucket):3d} trades, WR {wr:.1%}")


if __name__ == "__main__":
    main()
