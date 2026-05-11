"""Range Trading: adaptive trailing stop + BB squeeze + volume spike filters."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research.range_rsi_filter import (
    RangeRSIStrategy, LOOKBACK, MAX_HOLD, SWING_ORDER, compute_rsi,
    MIN_CORRIDOR_PCT, MIN_RR, SL_BUFFER_PCT, COOLDOWN, MIN_TOUCHES,
    TOLERANCE_PCT, MIN_LEVEL_AGE,
)
from src.strategy.levels import get_rolling_levels
from src.backtest.presets import bybit_futures
from src.backtest.metrics import compute_metrics

DATA_DIR = Path("data/processed/candles")
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ARBUSDT", "SUIUSDT",
]


class RangeAdaptiveTrail(RangeRSIStrategy):
    """Trail = corridor_width * trail_frac. Optional BB squeeze / vol spike filters."""

    def __init__(self, trail_frac=0.15, min_trail=0.3, max_trail=2.0,
                 bb_squeeze=False, vol_spike=False, fixed_trail=None):
        super().__init__()
        self.trail_frac = trail_frac
        self.min_trail = min_trail
        self.max_trail = max_trail
        self.bb_squeeze = bb_squeeze
        self.vol_spike = vol_spike
        self.fixed_trail = fixed_trail  # if set, ignore adaptive

    def generate_signals(self, symbol, df):
        signals = []
        cached_levels = []
        last_level_calc = -999
        last_signal_idx = -999
        rsi = compute_rsi(df["close"])
        vol_ma = df["volume"].rolling(20).mean()
        sma20 = df["close"].rolling(20).mean()
        std20 = df["close"].rolling(20).std()
        bb_width = (std20 / sma20 * 100).fillna(99)
        bb_width_ma = bb_width.rolling(50).mean().fillna(99)

        for idx in range(LOOKBACK + SWING_ORDER, len(df) - 1):
            if idx - last_signal_idx < COOLDOWN:
                continue
            if idx - last_level_calc >= 6:
                cached_levels = get_rolling_levels(
                    df, idx, lookback=LOOKBACK, min_touches=MIN_TOUCHES,
                    tolerance_pct=TOLERANCE_PCT, min_level_age=MIN_LEVEL_AGE,
                    swing_order=SWING_ORDER,
                )
                last_level_calc = idx
            if len(cached_levels) < 2:
                continue

            close = df["close"].iloc[idx]
            low = df["low"].iloc[idx]
            high = df["high"].iloc[idx]
            cur_vol_ratio = (
                df["volume"].iloc[idx] / vol_ma.iloc[idx]
                if vol_ma.iloc[idx] > 0 else 1
            )
            cur_bb = bb_width.iloc[idx]
            cur_bb_ma = bb_width_ma.iloc[idx]

            if self.bb_squeeze and cur_bb > cur_bb_ma * 0.8:
                continue
            if self.vol_spike and cur_vol_ratio < 1.5:
                continue

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

            if self.fixed_trail is not None:
                trail = self.fixed_trail
            else:
                trail = corridor_pct * self.trail_frac
                trail = max(self.min_trail, min(self.max_trail, trail))

            # LONG at support
            sup_zone_w = sup.zone_high - sup.zone_low
            touch_sup = sup.zone_high + sup_zone_w * 0.5
            if low <= touch_sup and close > sup.zone_low:
                sl = sup.zone_low * (1 - SL_BUFFER_PCT / 100)
                tp = res.zone_low
                risk = close - sl
                reward = tp - close
                if risk > 0 and reward / risk >= MIN_RR:
                    signals.append({
                        "entry_idx": idx, "entry_price": close,
                        "side": "long", "sl": sl, "tp": tp,
                        "metadata": {"trail_pct": trail,
                                     "corridor_pct": round(corridor_pct, 2),
                                     "rr": round(reward / risk, 2)},
                    })
                    last_signal_idx = idx
                    continue

            # SHORT at resistance
            res_zone_w = res.zone_high - res.zone_low
            touch_res = res.zone_low - res_zone_w * 0.5
            if high >= touch_res and close < res.zone_high:
                sl = res.zone_high * (1 + SL_BUFFER_PCT / 100)
                tp = sup.zone_high
                risk = sl - close
                reward = close - tp
                if risk > 0 and reward / risk >= MIN_RR:
                    signals.append({
                        "entry_idx": idx, "entry_price": close,
                        "side": "short", "sl": sl, "tp": tp,
                        "metadata": {"trail_pct": trail,
                                     "corridor_pct": round(corridor_pct, 2),
                                     "rr": round(reward / risk, 2)},
                    })
                    last_signal_idx = idx
        return signals

    def simulate_exit(self, df, signal, max_hold=200):
        entry_idx = signal["entry_idx"]
        entry_price = signal["entry_price"]
        is_long = signal["side"] == "long"
        sl = signal["sl"]
        tp = signal["tp"]
        trail_pct = signal.get("metadata", {}).get("trail_pct", 0.5)
        best_price = entry_price

        for i in range(entry_idx + 1, min(entry_idx + max_hold + 1, len(df))):
            h = df["high"].iloc[i]
            lo = df["low"].iloc[i]
            if is_long:
                if h > best_price:
                    best_price = h
                trail_sl = best_price * (1 - trail_pct / 100)
                sl = max(sl, trail_sl)
                if lo <= sl:
                    return {"exit_idx": i, "exit_price": sl, "reason": "sl"}
                if h >= tp:
                    return {"exit_idx": i, "exit_price": tp, "reason": "tp"}
            else:
                if lo < best_price:
                    best_price = lo
                trail_sl = best_price * (1 + trail_pct / 100)
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


def main():
    data = {}
    for sym in SYMBOLS:
        path = DATA_DIR / f"{sym}_4h.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            if len(df) >= LOOKBACK + 100:
                data[sym] = df

    train_d, test_d = {}, {}
    for sym, df in data.items():
        mid = int(len(df) * 0.5)
        train_d[sym] = df.iloc[:mid].reset_index(drop=True)
        test_d[sym] = df.iloc[mid:].reset_index(drop=True)

    cost = bybit_futures()

    configs = [
        # (trail_frac, bb, vol, fixed_trail, label)
        (None,  False, False, 0.5,  "Trail 0.5% fixed (baseline)"),
        (0.10,  False, False, None, "Adaptive 10% corridor"),
        (0.15,  False, False, None, "Adaptive 15% corridor"),
        (0.20,  False, False, None, "Adaptive 20% corridor"),
        (0.25,  False, False, None, "Adaptive 25% corridor"),
        (0.30,  False, False, None, "Adaptive 30% corridor"),
        (0.15,  True,  False, None, "Adapt 15% + BB squeeze"),
        (0.20,  True,  False, None, "Adapt 20% + BB squeeze"),
        (0.20,  False, True,  None, "Adapt 20% + Vol spike"),
        (0.20,  True,  True,  None, "Adapt 20% + BB + Vol"),
        (None,  True,  False, 0.5,  "Trail 0.5% + BB squeeze"),
        (None,  False, True,  0.5,  "Trail 0.5% + Vol spike"),
        (None,  True,  True,  0.5,  "Trail 0.5% + BB + Vol"),
    ]

    hdr = f"{'Config':33s} | {'N':>5s} {'WR':>6s} {'PF':>6s} {'Sharpe':>7s} {'Ann%':>7s} {'Exp%':>8s} {'t/mo':>5s}"
    print(hdr)
    print("-" * len(hdr))

    for frac, bb, vol, fixed, label in configs:
        s = RangeAdaptiveTrail(
            trail_frac=frac or 0.15,
            bb_squeeze=bb, vol_spike=vol,
            fixed_trail=fixed,
        )
        te = s.run(test_d, position_size=1000, max_hold=MAX_HOLD)
        cost.apply_all(te)
        if len(te) < 5:
            print(f"{label:33s} | {len(te):5d}  (too few)")
            continue
        m = compute_metrics(te)
        print(f"{label:33s} | {m.n_trades:5d} {m.win_rate:5.1%} {m.profit_factor:6.2f} "
              f"{m.sharpe:7.2f} {m.annual_return_pct:+6.1f}% {m.expectancy_pct:+7.4f}% "
              f"{m.trades_per_month:5.1f}")


if __name__ == "__main__":
    main()
