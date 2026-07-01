"""Big Move Detector + tick bar features experiment.

Original big move detector predicts TIMING well (precision 80%)
but direction poorly (RSI-based, WR 36-45%).

Hypothesis: tick bar features (sell%, duration, imbalance) can
improve direction prediction when big move is detected.

Approach:
  1. Load 1h candles (existing) + aggTrades from Binance Vision
  2. When big move model fires (P>threshold), look at recent tick bars
  3. Test: does sell%/buy% pressure predict direction better than RSI?

Usage:
    python scripts/research/bigmove_tick_filter.py [--days 90]
"""
import csv
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data" / "processed" / "candles"
CACHE_DIR = ROOT / "data" / "cache" / "aggtrades"

# Override if running from /tmp
if not DATA.exists():
    ROOT = Path("/root/trading")
    DATA = ROOT / "data" / "processed" / "candles"
    CACHE_DIR = ROOT / "data" / "cache" / "aggtrades"

BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"

SYMBOLS = ["SOL", "DOGE", "SUI", "AVAX"]  # skip ETH (too large)
TICK_SIZE = 500
FEE_BPS = 10

# Big move detection params (from backtest_big_move.py)
MOVE_THRESHOLD = 5.0   # % move to count as "big move"
MOVE_WINDOW = 6        # candles (hours) to look ahead
SL_PCT = 2.5
RR = 2.0
MAX_HOLD = 12


# ======================================================================
# Data loading
# ======================================================================

def load_candles(symbol: str) -> pd.DataFrame | None:
    """Load 1h candles from parquet."""
    key = f"{symbol}USDT"
    path = DATA / f"{key}_1h.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    return df


def ensure_cached(symbol: str, date_str: str) -> Path | None:
    """Download aggTrades ZIP if not cached."""
    cache_path = CACHE_DIR / f"{symbol}-{date_str}.csv"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    url = f"{BASE_URL}/{symbol}USDT/{symbol}USDT-aggTrades-{date_str}.zip"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=30)
        data = resp.read()
    except (HTTPError, Exception):
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        csv_name = zf.namelist()[0]
        csv_data = zf.read(csv_name)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(csv_data)
        return cache_path
    except Exception:
        return None


def compute_tick_features_for_hour(csv_path: Path, hour_start_ms: int,
                                    hour_end_ms: int,
                                    tick_size: int = 500) -> dict | None:
    """Compute tick bar features for trades within a specific hour.
    Streams through CSV, only keeping trades in [hour_start, hour_end)."""
    trades = []
    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            try:
                ts = int(row[5])
                if ts < hour_start_ms:
                    continue
                if ts >= hour_end_ms:
                    break  # CSV is sorted by time
                trades.append((
                    ts,
                    float(row[1]),   # price
                    float(row[2]),   # qty
                    row[6].strip().lower() == "true",  # is_sell
                ))
            except (ValueError, IndexError):
                continue

    if len(trades) < tick_size:
        return None

    # Build tick bars for this hour
    bars = []
    for i in range(0, len(trades) - tick_size + 1, tick_size):
        chunk = trades[i:i + tick_size]
        prices = [t[1] for t in chunk]
        qty = sum(t[2] for t in chunk)
        sell_qty = sum(t[2] for t in chunk if t[3])
        bars.append({
            "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
            "duration_s": (chunk[-1][0] - chunk[0][0]) / 1000,
            "ret": (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0,
            "range_pct": (max(prices) - min(prices)) / prices[0] * 100 if prices[0] > 0 else 0,
            "volume": qty,
        })

    if not bars:
        return None

    # Aggregate tick bar features for this hour
    sell_pcts = [b["sell_pct"] for b in bars]
    durations = [b["duration_s"] for b in bars]
    rets = [b["ret"] for b in bars]

    return {
        "n_bars": len(bars),
        "n_trades": len(trades),
        "avg_sell_pct": np.mean(sell_pcts),
        "max_sell_pct": max(sell_pcts),
        "min_sell_pct": min(sell_pcts),
        "sell_pct_std": np.std(sell_pcts),
        "avg_duration_s": np.mean(durations),
        "min_duration_s": min(durations),
        "avg_bar_ret": np.mean(rets),
        "max_bar_ret": max(rets),
        "min_bar_ret": min(rets),
        "bar_ret_std": np.std(rets),
        # Directional pressure
        "net_sell_pressure": np.mean(sell_pcts) - 50,  # >0 = selling, <0 = buying
        "sell_bars_pct": sum(1 for s in sell_pcts if s > 60) / len(sell_pcts) * 100,
        "buy_bars_pct": sum(1 for s in sell_pcts if s < 40) / len(sell_pcts) * 100,
        # Activity
        "activity_ratio": min(durations) / max(max(durations), 1),  # 1=uniform, 0=bursty
    }


# ======================================================================
# Feature extraction (simplified from backtest_big_move.py)
# ======================================================================

def extract_candle_features(df: pd.DataFrame, idx: int, lookback: int = 24) -> dict:
    """Extract standard candle features at idx."""
    if idx < lookback + 10:
        return {}

    f = {}
    window = df.iloc[idx - lookback : idx]
    close = df["close"].iloc[idx]

    f["return_24h"] = (close / df["close"].iloc[idx - lookback] - 1) * 100
    f["return_12h"] = (close / df["close"].iloc[idx - lookback // 2] - 1) * 100
    f["return_6h"] = (close / df["close"].iloc[idx - lookback // 4] - 1) * 100
    f["abs_return_24h"] = abs(f["return_24h"])

    returns = window["close"].pct_change().dropna()
    f["volatility"] = float(returns.std() * 100) if len(returns) > 1 else 0

    f["range_pct"] = (window["high"].max() - window["low"].min()) / close * 100

    avg_vol = df["volume"].iloc[max(0, idx - lookback * 3) : idx - lookback].mean()
    recent_vol = window["volume"].mean()
    f["volume_ratio"] = float(recent_vol / avg_vol) if avg_vol > 0 else 1.0
    f["volume_spike"] = float(window["volume"].max() / avg_vol) if avg_vol > 0 else 1.0

    # RSI
    if idx >= 14:
        deltas = df["close"].iloc[idx - 14 : idx + 1].diff().dropna()
        gains = deltas.clip(lower=0).mean()
        losses_v = (-deltas.clip(upper=0)).mean()
        rs = gains / losses_v if losses_v > 0 else 100
        f["rsi"] = 100 - (100 / (1 + rs))
    else:
        f["rsi"] = 50

    # Volatility contraction
    if idx >= 40:
        std_20 = df["close"].iloc[idx - 20 : idx].std()
        std_prev = df["close"].iloc[idx - 40 : idx - 20].std()
        f["volatility_contraction"] = float(std_20 / std_prev) if std_prev > 0 else 1.0
    else:
        f["volatility_contraction"] = 1.0

    return f


# ======================================================================
# Direction prediction comparison
# ======================================================================

def run_experiment(days: int = 90):
    """Compare direction predictors when big move is detected."""

    print("=" * 70)
    print("  BIG MOVE + TICK BAR DIRECTION FILTER")
    print("=" * 70)
    print(f"  Days: {days},  Coins: {SYMBOLS}")
    print(f"  Move threshold: {MOVE_THRESHOLD}%,  Window: {MOVE_WINDOW}h")
    print(f"  Tick size: {TICK_SIZE}\n")

    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    all_events = []

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---", flush=True)

        # Load candles
        df = load_candles(symbol)
        if df is None:
            print(f"  No candle data, skipping")
            continue

        # Parse timestamps
        if "ts" in df.columns:
            if df["ts"].dtype == "int64":
                df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            else:
                df["dt"] = pd.to_datetime(df["ts"], utc=True)
        elif "datetime" in df.columns:
            df["dt"] = pd.to_datetime(df["datetime"], utc=True)
        else:
            print(f"  No timestamp column, skipping")
            continue

        # Filter to our date range
        mask = (df["dt"].dt.date >= start_date) & (df["dt"].dt.date <= end_date)
        if mask.sum() < 100:
            print(f"  Not enough data in range, skipping")
            continue

        # Find big move events (label=1)
        events = 0
        last_event = -MOVE_WINDOW * 2

        for i in range(50, len(df) - MOVE_WINDOW - 1):
            if not mask.iloc[i]:
                continue
            if i - last_event < MOVE_WINDOW * 2:
                continue

            entry = df["close"].iloc[i]
            future_high = df["high"].iloc[i + 1 : i + 1 + MOVE_WINDOW].max()
            future_low = df["low"].iloc[i + 1 : i + 1 + MOVE_WINDOW].min()
            up = (future_high - entry) / entry * 100
            down = (entry - future_low) / entry * 100

            if up < MOVE_THRESHOLD and down < MOVE_THRESHOLD:
                continue

            # This IS a big move. What direction?
            actual_dir = "up" if up > down else "down"
            actual_move = max(up, down)
            last_event = i

            # Get candle features
            feat = extract_candle_features(df, i)
            if not feat:
                continue

            # RSI direction prediction
            rsi = feat["rsi"]
            rsi_pred = "up" if rsi > 55 else ("down" if rsi < 45 else "neutral")

            # Get tick bar features for this hour
            candle_dt = df["dt"].iloc[i]
            date_str = candle_dt.strftime("%Y-%m-%d")
            hour_start_ms = int(candle_dt.timestamp() * 1000)
            hour_end_ms = hour_start_ms + 3600_000

            # Also check previous hour for lead signal
            prev_hour_start = hour_start_ms - 3600_000
            prev_date_str = (candle_dt - timedelta(hours=1)).strftime("%Y-%m-%d")

            tick_feat = None
            for ds, hs, he in [(date_str, hour_start_ms, hour_end_ms),
                                (prev_date_str, prev_hour_start, hour_start_ms)]:
                csv_path = ensure_cached(symbol, ds)
                if csv_path:
                    tf = compute_tick_features_for_hour(csv_path, hs, he, TICK_SIZE)
                    if tf:
                        tick_feat = tf
                        break

            # Tick-based direction prediction
            tick_pred = "neutral"
            if tick_feat:
                nsp = tick_feat["net_sell_pressure"]
                if nsp > 5:
                    tick_pred = "down"   # selling pressure → price going down
                elif nsp < -5:
                    tick_pred = "up"     # buying pressure → price going up
                # Also try: opposite (contrarian)

            event = {
                "symbol": symbol,
                "date": candle_dt.strftime("%Y-%m-%d %H:%M"),
                "actual_dir": actual_dir,
                "actual_move": actual_move,
                "rsi": rsi,
                "rsi_pred": rsi_pred,
                "tick_pred": tick_pred,
                "tick_feat": tick_feat,
                **feat,
            }
            all_events.append(event)
            events += 1

        print(f"  Big move events: {events}", flush=True)

    if not all_events:
        print("\nNo events found!")
        return

    # ======================================================================
    # Analysis
    # ======================================================================

    print(f"\n\n{'='*70}")
    print(f"  DIRECTION PREDICTION COMPARISON")
    print(f"  {len(all_events)} big move events across {len(SYMBOLS)} coins")
    print(f"{'='*70}\n")

    # 1. RSI-based direction
    rsi_trades = [e for e in all_events if e["rsi_pred"] != "neutral"]
    rsi_correct = sum(1 for e in rsi_trades if e["rsi_pred"] == e["actual_dir"])
    rsi_wr = rsi_correct / len(rsi_trades) * 100 if rsi_trades else 0

    print(f"  RSI direction (>55 long, <45 short):")
    print(f"    Trades: {len(rsi_trades)}/{len(all_events)} ({len(all_events)-len(rsi_trades)} skipped neutral)")
    print(f"    Correct: {rsi_correct}/{len(rsi_trades)} = {rsi_wr:.1f}%\n")

    # 2. Tick-based direction (momentum: follow pressure)
    tick_trades = [e for e in all_events if e["tick_pred"] != "neutral"]
    tick_correct = sum(1 for e in tick_trades if e["tick_pred"] == e["actual_dir"])
    tick_wr = tick_correct / len(tick_trades) * 100 if tick_trades else 0

    print(f"  Tick pressure direction (sell%>55 → short, <45 → long):")
    print(f"    Trades: {len(tick_trades)}/{len(all_events)} ({len(all_events)-len(tick_trades)} skipped neutral)")
    print(f"    Correct: {tick_correct}/{len(tick_trades)} = {tick_wr:.1f}%\n")

    # 3. Contrarian tick (opposite of pressure)
    tick_contra = [e for e in all_events if e["tick_pred"] != "neutral"]
    contra_correct = sum(1 for e in tick_contra
                         if (e["tick_pred"] == "down" and e["actual_dir"] == "up") or
                            (e["tick_pred"] == "up" and e["actual_dir"] == "down"))
    contra_wr = contra_correct / len(tick_contra) * 100 if tick_contra else 0

    print(f"  Tick CONTRARIAN (sell pressure → go LONG, buy → SHORT):")
    print(f"    Correct: {contra_correct}/{len(tick_contra)} = {contra_wr:.1f}%\n")

    # 4. Combined: RSI + tick agreement
    both = [e for e in all_events if e["rsi_pred"] != "neutral" and e["tick_pred"] != "neutral"]
    agree = [e for e in both if e["rsi_pred"] == e["tick_pred"]]
    disagree = [e for e in both if e["rsi_pred"] != e["tick_pred"]]

    if agree:
        agree_correct = sum(1 for e in agree if e["rsi_pred"] == e["actual_dir"])
        print(f"  RSI + Tick AGREE (same direction):")
        print(f"    Trades: {len(agree)}")
        print(f"    Correct: {agree_correct}/{len(agree)} = {agree_correct/len(agree)*100:.1f}%\n")

    if disagree:
        # When they disagree, who's right?
        rsi_right = sum(1 for e in disagree if e["rsi_pred"] == e["actual_dir"])
        tick_right = sum(1 for e in disagree if e["tick_pred"] == e["actual_dir"])
        print(f"  RSI + Tick DISAGREE ({len(disagree)} events):")
        print(f"    RSI right: {rsi_right} ({rsi_right/len(disagree)*100:.0f}%)")
        print(f"    Tick right: {tick_right} ({tick_right/len(disagree)*100:.0f}%)\n")

    # 5. Tick sell% thresholds
    print(f"\n  SELL% THRESHOLD SCAN:\n")
    events_with_tick = [e for e in all_events if e["tick_feat"]]
    if events_with_tick:
        print(f"  {'Threshold':<20s} {'N':>4s} {'Dir WR':>7s} {'Avg move':>9s}")
        print(f"  " + "-" * 45)

        for label, condition in [
            ("sell% > 70 → SHORT", lambda e: e["tick_feat"]["avg_sell_pct"] > 70),
            ("sell% > 60 → SHORT", lambda e: e["tick_feat"]["avg_sell_pct"] > 60),
            ("sell% < 40 → LONG",  lambda e: e["tick_feat"]["avg_sell_pct"] < 40),
            ("sell% < 30 → LONG",  lambda e: e["tick_feat"]["avg_sell_pct"] < 30),
            ("fast bars → momentum", lambda e: e["tick_feat"]["min_duration_s"] < 10),
            ("slow bars → reversal", lambda e: e["tick_feat"]["avg_duration_s"] > 120),
            ("high ret_std → volatile", lambda e: e["tick_feat"]["bar_ret_std"] > 0.3),
            ("sell_bars > 60%", lambda e: e["tick_feat"]["sell_bars_pct"] > 60),
            ("buy_bars > 60%", lambda e: e["tick_feat"]["buy_bars_pct"] > 60),
        ]:
            filtered = [e for e in events_with_tick if condition(e)]
            if not filtered:
                continue

            # For sell% → SHORT: correct if actual is down
            if "SHORT" in label:
                correct = sum(1 for e in filtered if e["actual_dir"] == "down")
            elif "LONG" in label:
                correct = sum(1 for e in filtered if e["actual_dir"] == "up")
            else:
                # For activity-based: use RSI direction
                correct = sum(1 for e in filtered
                              if (e["rsi"] > 55 and e["actual_dir"] == "up") or
                                 (e["rsi"] < 45 and e["actual_dir"] == "down"))

            wr = correct / len(filtered) * 100
            avg_m = np.mean([e["actual_move"] for e in filtered])
            print(f"  {label:<20s} {len(filtered):>4d} {wr:>6.1f}% {avg_m:>+8.2f}%")

    # Simulate P&L for each direction method
    print(f"\n\n{'='*70}")
    print(f"  SIMULATED P&L (SL={SL_PCT}%, TP={SL_PCT*RR}%, max_hold={MAX_HOLD}h)")
    print(f"{'='*70}\n")

    for method_name, pred_fn in [
        ("RSI only", lambda e: e["rsi_pred"]),
        ("Tick momentum", lambda e: e["tick_pred"]),
        ("Tick contrarian", lambda e: "up" if e["tick_pred"] == "down" else ("down" if e["tick_pred"] == "up" else "neutral")),
    ]:
        trades = []
        for e in all_events:
            pred = pred_fn(e)
            if pred == "neutral":
                continue

            is_long = pred == "up"
            actual = e["actual_move"]
            if (is_long and e["actual_dir"] == "up") or (not is_long and e["actual_dir"] == "down"):
                # Correct direction — may hit TP or SL
                if actual >= SL_PCT * RR:
                    pnl = SL_PCT * RR - FEE_BPS / 100 * 2
                else:
                    pnl = min(actual, SL_PCT * RR) * 0.6 - FEE_BPS / 100 * 2  # partial capture
            else:
                # Wrong direction
                pnl = -SL_PCT - FEE_BPS / 100 * 2

            trades.append(pnl)

        if trades:
            trades = np.array(trades)
            wr = (trades > 0).mean() * 100
            avg = trades.mean()
            total = trades.sum()
            pf = abs(trades[trades > 0].sum() / trades[trades < 0].sum()) if (trades < 0).any() else float('inf')
            print(f"  {method_name:<20s}: N={len(trades):>3d} WR={wr:.0f}% "
                  f"avg={avg:+.2f}% total={total:+.1f}% PF={pf:.2f}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    run_experiment(args.days)


if __name__ == "__main__":
    main()
