"""Sentiment filter for Miro signals — Fear & Greed Index + Claude scoring.

Step 1: Run Miro backtest → get all historical signals with dates
Step 2: Fetch Fear & Greed Index for each signal date
Step 3: Analyze: does market sentiment improve signal quality?
Step 4: (Future) Add per-coin sentiment from CryptoPanic/LunarCrush
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol
from src.strategy.features import compute_features
from src.strategy.models import Signal, SignalType
from src.backtest.presets import bybit_futures
from src.backtest.runner import CandleStrategy
from src.backtest.metrics import compute_metrics

# Config — same as screener
DATA_DIR = Path("data/processed/candles")
SYMBOLS_4H = [f.stem.replace("_4h", "") for f in DATA_DIR.glob("*_4h.parquet")]

LOOKBACK = 200
SWING_ORDER = 5
MIN_TOUCHES = 2
TOLERANCE_PCT = 1.0
MIN_LEVEL_AGE = 10
SL_PCT = 4.0
RR_RATIO = 3.0
COOLDOWN = 6


class MiroBacktestStrategy(CandleStrategy):
    """Simplified Miro strategy for backtesting — breakout + retest + zakol."""

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> list[dict]:
        signals = []
        cached_levels = []
        last_level_calc = -999
        last_signal_idx = -999

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

            if not cached_levels:
                continue

            close = df["close"].iloc[idx]
            high = df["high"].iloc[idx]
            low = df["low"].iloc[idx]

            # Detect patterns
            breakouts = detect_breakouts(df, idx, cached_levels)
            for brk in breakouts:
                retests = detect_retests(df, idx, [brk], retest_window=6)
                zakols = detect_zakol(df, idx, cached_levels)

                for sig_type, detected in [("retest", retests), ("zakol", zakols)]:
                    if not detected:
                        continue

                    # detected is (SignalType, Level, score)
                    sig_enum, level, score = detected
                    is_long = "long" in sig_enum.value
                    entry = close

                    if is_long:
                        sl = entry * (1 - SL_PCT / 100)
                        tp = entry * (1 + SL_PCT * RR_RATIO / 100)
                    else:
                        sl = entry * (1 + SL_PCT / 100)
                        tp = entry * (1 - SL_PCT * RR_RATIO / 100)

                    ts = df["ts"].iloc[idx] if "ts" in df.columns else None

                    signals.append({
                        "entry_idx": idx,
                        "entry_price": entry,
                        "side": "long" if is_long else "short",
                        "sl": sl,
                        "tp": tp,
                        "metadata": {
                            "type": sig_enum.value,
                            "level_price": level.price,
                            "level_score": level.score,
                            "level_touches": level.touches,
                            "timestamp": str(ts) if ts else "",
                        },
                    })
                    last_signal_idx = idx
                    break
                if idx == last_signal_idx:
                    break

        return signals


def fetch_fear_greed_history(limit: int = 730) -> dict[str, int]:
    """Fetch Fear & Greed Index history. Returns {YYYY-MM-DD: value}."""
    url = f"https://api.alternative.me/fng/?limit={limit}&format=json"
    with urlopen(url) as resp:
        data = json.loads(resp.read())

    result = {}
    for entry in data.get("data", []):
        ts = int(entry["timestamp"])
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        result[date_str] = int(entry["value"])
    return result


def main():
    print("Fetching Fear & Greed Index history...")
    fng = fetch_fear_greed_history(limit=730)
    print(f"  Got {len(fng)} days ({min(fng.keys())} to {max(fng.keys())})")

    print("\nLoading candle data...")
    data = {}
    for sym in SYMBOLS_4H:
        path = DATA_DIR / f"{sym}_4h.parquet"
        df = pd.read_parquet(path)
        if len(df) < LOOKBACK + 100:
            continue
        data[sym] = df
    print(f"  Loaded {len(data)} symbols")

    # Split 50/50
    train_d, test_d = {}, {}
    for sym, df in data.items():
        mid = int(len(df) * 0.5)
        train_d[sym] = df.iloc[:mid].reset_index(drop=True)
        test_d[sym] = df.iloc[mid:].reset_index(drop=True)

    cost = bybit_futures()
    strategy = MiroBacktestStrategy()

    # Run on TEST only
    print("\nRunning Miro backtest on TEST set...")
    test_trades = strategy.run(test_d, position_size=1000, max_hold=60)
    cost.apply_all(test_trades)
    print(f"  {len(test_trades)} trades")

    if not test_trades:
        print("No trades!")
        return

    # Match trades with Fear & Greed
    matched = []
    for t in test_trades:
        ts_str = (t.metadata or {}).get("timestamp", "")
        if not ts_str:
            continue
        # Parse timestamp to date
        try:
            dt = pd.Timestamp(ts_str)
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            continue
        fng_val = fng.get(date_str)
        if fng_val is None:
            continue
        matched.append((t, fng_val))

    print(f"\nMatched with F&G: {len(matched)}/{len(test_trades)} trades")

    if len(matched) < 20:
        print("Not enough matched trades!")
        return

    # Baseline metrics
    all_trades = [t for t, _ in matched]
    m = compute_metrics(all_trades)
    print(f"\nBaseline: N={m.n_trades} WR={m.win_rate:.1%} PF={m.profit_factor:.2f} "
          f"Sharpe={m.sharpe:.2f} Ann={m.annual_return_pct:+.1f}%")

    # F&G bucket analysis
    print(f"\n{'F&G Range':15s} {'N':>5s} {'WR':>6s} {'PF':>6s} {'AvgPnL':>8s} {'Sharpe':>7s}")
    print("-" * 50)
    for lo, hi, label in [
        (0, 25, "Extreme Fear"),
        (25, 40, "Fear"),
        (40, 55, "Neutral"),
        (55, 75, "Greed"),
        (75, 101, "Extreme Greed"),
    ]:
        bucket = [t for t, fg in matched if lo <= fg < hi]
        if len(bucket) < 5:
            print(f"{label:15s} {len(bucket):5d}  (too few)")
            continue
        bm = compute_metrics(bucket)
        print(f"{label:15s} {bm.n_trades:5d} {bm.win_rate:5.1%} {bm.profit_factor:6.2f} "
              f"{bm.expectancy_pct:+7.3f}% {bm.sharpe:7.2f}")

    # Long vs Short in different sentiment regimes
    print(f"\n{'Regime':15s} {'Side':>6s} {'N':>5s} {'WR':>6s} {'PF':>6s} {'AvgPnL':>8s}")
    print("-" * 50)
    for lo, hi, label in [(0, 40, "Fear(<40)"), (40, 60, "Neutral"), (60, 101, "Greed(>60)")]:
        for side in ["long", "short"]:
            bucket = [t for t, fg in matched if lo <= fg < hi and t.side.value == side]
            if len(bucket) < 5:
                continue
            bm = compute_metrics(bucket)
            print(f"{label:15s} {side:>6s} {bm.n_trades:5d} {bm.win_rate:5.1%} "
                  f"{bm.profit_factor:6.2f} {bm.expectancy_pct:+7.3f}%")

    # Optimal filter analysis
    print(f"\n{'Filter':30s} {'N':>5s} {'WR':>6s} {'PF':>6s} {'Sharpe':>7s} {'Ann%':>7s}")
    print("-" * 65)

    # No filter
    print(f"{'No filter':30s} {m.n_trades:5d} {m.win_rate:5.1%} {m.profit_factor:6.2f} "
          f"{m.sharpe:7.2f} {m.annual_return_pct:+6.1f}%")

    # Filter: long only in Fear, short only in Greed
    contrarian = [t for t, fg in matched
                  if (t.side.value == "long" and fg < 40)
                  or (t.side.value == "short" and fg > 60)]
    if len(contrarian) >= 5:
        cm = compute_metrics(contrarian)
        print(f"{'Contrarian (L<40, S>60)':30s} {cm.n_trades:5d} {cm.win_rate:5.1%} "
              f"{cm.profit_factor:6.2f} {cm.sharpe:7.2f} {cm.annual_return_pct:+6.1f}%")

    # Filter: long only in Greed (momentum), short in Fear
    momentum = [t for t, fg in matched
                if (t.side.value == "long" and fg > 55)
                or (t.side.value == "short" and fg < 45)]
    if len(momentum) >= 5:
        mm = compute_metrics(momentum)
        print(f"{'Momentum (L>55, S<45)':30s} {mm.n_trades:5d} {mm.win_rate:5.1%} "
              f"{mm.profit_factor:6.2f} {mm.sharpe:7.2f} {mm.annual_return_pct:+6.1f}%")

    # Skip extreme greed for longs (bubble top)
    no_fomo = [t for t, fg in matched
               if not (t.side.value == "long" and fg > 75)]
    if len(no_fomo) >= 5:
        nm = compute_metrics(no_fomo)
        print(f"{'Skip LONG in Extreme Greed':30s} {nm.n_trades:5d} {nm.win_rate:5.1%} "
              f"{nm.profit_factor:6.2f} {nm.sharpe:7.2f} {nm.annual_return_pct:+6.1f}%")

    # Skip extreme fear for shorts (capitulation bounce)
    no_panic = [t for t, fg in matched
                if not (t.side.value == "short" and fg < 25)]
    if len(no_panic) >= 5:
        npm = compute_metrics(no_panic)
        print(f"{'Skip SHORT in Extreme Fear':30s} {npm.n_trades:5d} {npm.win_rate:5.1%} "
              f"{npm.profit_factor:6.2f} {npm.sharpe:7.2f} {npm.annual_return_pct:+6.1f}%")

    # Correlation
    fng_vals = [fg for _, fg in matched]
    wins = [1 if t.net_pnl_pct > 0 else 0 for t, _ in matched]
    pnls = [t.net_pnl_pct for t, _ in matched]
    corr_win = np.corrcoef(fng_vals, wins)[0, 1]
    corr_pnl = np.corrcoef(fng_vals, pnls)[0, 1]
    print(f"\nCorrelations: F&G vs win: {corr_win:+.3f}, F&G vs PnL: {corr_pnl:+.3f}")


if __name__ == "__main__":
    main()
