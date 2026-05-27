#!/usr/bin/env python3
"""CVD (Cumulative Volume Delta) as a feature for Miro strategy.

Hypothesis: CVD divergence on 4H timeframe detects institutional accumulation/
distribution that tick-level buy/sell% misses. Sustained CVD trend while price
is flat → directional signal.

Data: Binance Futures klines (has taker_buy_volume column).
Test: add CVD features to Miro ML pipeline, measure importance & WR impact.

Usage:
    python scripts/research/cvd_feature_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ── Binance Futures klines (includes taker buy volume) ──────────────

BINANCE_FAPI = "https://fapi.binance.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT",
           "LINKUSDT", "AVAXUSDT", "ADAUSDT"]

CACHE_DIR = Path("data/cache/cvd")


def fetch_klines_binance(symbol: str, interval: str = "4h",
                         months: int = 12) -> pd.DataFrame:
    """Fetch klines from Binance Futures with taker_buy_volume."""
    cache_path = CACHE_DIR / f"{symbol}_{interval}_{months}m.parquet"
    if cache_path.exists():
        print(f"  [cache] {symbol}")
        return pd.read_parquet(cache_path)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - months * 30 * 24 * 3600 * 1000
    all_rows = []
    cur = start_ms

    print(f"  Downloading {symbol} {interval}...", end="", flush=True)
    while cur < end_ms:
        url = (f"{BINANCE_FAPI}/fapi/v1/klines"
               f"?symbol={symbol}&interval={interval}&startTime={cur}&limit=1500")
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f" error: {e}")
            break

        if not data:
            break

        all_rows.extend(data)
        cur = data[-1][0] + 1
        time.sleep(0.15)

    if not all_rows:
        print(" empty")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base_vol", "taker_buy_quote_vol", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume",
                "taker_buy_base_vol", "taker_buy_quote_vol"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    print(f" {len(df)} candles")
    return df


# ── CVD features ────────────────────────────────────────────────────

def add_cvd_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add CVD-based features to OHLCV DataFrame."""
    # Volume delta per candle = taker_buy - taker_sell
    # taker_sell = total_volume - taker_buy
    df = df.copy()
    df["taker_sell_vol"] = df["volume"] - df["taker_buy_base_vol"]
    df["vol_delta"] = df["taker_buy_base_vol"] - df["taker_sell_vol"]

    # CVD = cumulative volume delta
    df["cvd"] = df["vol_delta"].cumsum()

    # ── Feature 1: CVD slope (linear reg over N candles) ──
    for lookback in [6, 12, 24]:  # 24h, 48h, 96h on 4H
        slopes = []
        for i in range(len(df)):
            if i < lookback:
                slopes.append(0.0)
                continue
            y = df["cvd"].iloc[i - lookback + 1: i + 1].values
            x = np.arange(lookback)
            # normalize by avg volume to make cross-coin comparable
            avg_vol = df["volume"].iloc[i - lookback + 1: i + 1].mean()
            if avg_vol > 0:
                slope = np.polyfit(x, y, 1)[0] / avg_vol
            else:
                slope = 0.0
            slopes.append(slope)
        df[f"cvd_slope_{lookback}"] = slopes

    # ── Feature 2: CVD-price divergence ──
    # Price direction vs CVD direction over last N candles
    for lookback in [6, 12, 24]:
        price_ret = df["close"].pct_change(lookback)
        cvd_change = df["cvd"].diff(lookback)
        avg_vol = df["volume"].rolling(lookback).mean()
        cvd_norm = cvd_change / avg_vol.replace(0, np.nan)
        # Divergence: CVD going up while price flat/down (accumulation)
        # or CVD going down while price flat/up (distribution)
        df[f"cvd_price_diverg_{lookback}"] = cvd_norm - price_ret * 10

    # ── Feature 3: Buy ratio (taker_buy / total volume) ──
    for lookback in [6, 12, 24]:
        buy_sum = df["taker_buy_base_vol"].rolling(lookback).sum()
        vol_sum = df["volume"].rolling(lookback).sum()
        df[f"buy_ratio_{lookback}"] = buy_sum / vol_sum.replace(0, np.nan) - 0.5

    # ── Feature 4: Vol delta Z-score ──
    for lookback in [24, 48]:
        rolling_mean = df["vol_delta"].rolling(lookback).mean()
        rolling_std = df["vol_delta"].rolling(lookback).std()
        df[f"vol_delta_zscore_{lookback}"] = (
            (df["vol_delta"] - rolling_mean) / rolling_std.replace(0, np.nan)
        )

    return df


# ── Miro-like signal detection (simplified for feature test) ────────

def detect_signals(df: pd.DataFrame, min_touches: int = 2,
                   zone_pct: float = 0.5) -> list[dict]:
    """Simplified S/R level detection + breakout/retest signals."""
    signals = []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    # Find support/resistance via pivot points
    for i in range(20, n - 1):
        # Check if we have a recent level (simple: local high/low in last 60 candles)
        window = slice(max(0, i - 60), i)
        local_high = highs[window].max()
        local_low = lows[window].min()

        # Count touches for resistance
        zone_h = local_high * zone_pct / 100
        touches_high = np.sum(np.abs(highs[window] - local_high) < zone_h)
        touches_low = np.sum(np.abs(lows[window] - local_low) < zone_h)

        price = closes[i]

        # Breakout above resistance
        if touches_high >= min_touches and price > local_high:
            prev_close = closes[i - 1]
            if prev_close <= local_high:  # fresh breakout
                signals.append({
                    "idx": i,
                    "type": "long_breakout",
                    "level": local_high,
                    "price": price,
                })

        # Breakdown below support
        if touches_low >= min_touches and price < local_low:
            prev_close = closes[i - 1]
            if prev_close >= local_low:
                signals.append({
                    "idx": i,
                    "type": "short_breakout",
                    "level": local_low,
                    "price": price,
                })

    return signals


def evaluate_signal(df: pd.DataFrame, idx: int, direction: str,
                    hold_candles: int = 6) -> dict:
    """Evaluate signal outcome: hold for N candles, measure PnL."""
    if idx + hold_candles >= len(df):
        return {"pnl": np.nan, "mfe": np.nan, "mae": np.nan}

    entry = df["close"].iloc[idx]
    future = df["close"].iloc[idx + 1: idx + hold_candles + 1]
    future_h = df["high"].iloc[idx + 1: idx + hold_candles + 1]
    future_l = df["low"].iloc[idx + 1: idx + hold_candles + 1]

    if direction == "long":
        pnl = (future.iloc[-1] - entry) / entry * 100
        mfe = (future_h.max() - entry) / entry * 100
        mae = (entry - future_l.min()) / entry * 100
    else:
        pnl = (entry - future.iloc[-1]) / entry * 100
        mfe = (entry - future_l.min()) / entry * 100
        mae = (future_h.max() - entry) / entry * 100

    return {"pnl": pnl, "mfe": mfe, "mae": mae}


# ── Main: test CVD features ────────────────────────────────────────

CVD_FEATURES = [
    "cvd_slope_6", "cvd_slope_12", "cvd_slope_24",
    "cvd_price_diverg_6", "cvd_price_diverg_12", "cvd_price_diverg_24",
    "buy_ratio_6", "buy_ratio_12", "buy_ratio_24",
    "vol_delta_zscore_24", "vol_delta_zscore_48",
]


def main():
    print("=" * 70)
    print("CVD Feature Test for Miro-like Signals")
    print("=" * 70)

    # 1. Download data
    print("\n── Downloading 4H klines with taker_buy volume ──")
    dfs = {}
    for sym in SYMBOLS:
        df = fetch_klines_binance(sym, "4h", 12)
        if not df.empty:
            df = add_cvd_features(df)
            dfs[sym] = df

    # 2. Generate signals & extract features
    print("\n── Detecting signals & extracting CVD features ──")
    all_rows = []
    for sym, df in dfs.items():
        signals = detect_signals(df)
        for sig in signals:
            idx = sig["idx"]
            direction = "long" if "long" in sig["type"] else "short"
            outcome = evaluate_signal(df, idx, direction, hold_candles=6)
            if np.isnan(outcome["pnl"]):
                continue

            row = {
                "symbol": sym,
                "idx": idx,
                "ts": df["ts"].iloc[idx],
                "type": sig["type"],
                "direction": direction,
                "price": sig["price"],
                "pnl": outcome["pnl"],
                "mfe": outcome["mfe"],
                "mae": outcome["mae"],
                "win": 1 if outcome["pnl"] > 0 else 0,
            }
            # Add CVD features
            for feat in CVD_FEATURES:
                row[feat] = df[feat].iloc[idx] if feat in df.columns else 0.0

            all_rows.append(row)

        print(f"  {sym}: {len(signals)} signals")

    if not all_rows:
        print("No signals found!")
        return

    results = pd.DataFrame(all_rows)
    print(f"\nTotal signals: {len(results)}, WR: {results['win'].mean():.1%}")

    # 3. Feature importance (correlation with outcome)
    print("\n── CVD Feature Correlations with PnL ──")
    print(f"{'Feature':<30} {'corr(PnL)':>10} {'corr(win)':>10} {'mean(W)':>10} {'mean(L)':>10}")
    print("-" * 75)

    importances = []
    for feat in CVD_FEATURES:
        vals = results[feat].replace([np.inf, -np.inf], np.nan).dropna()
        matched = results.loc[vals.index]
        if len(vals) < 20:
            continue
        corr_pnl = vals.corr(matched["pnl"])
        corr_win = vals.corr(matched["win"])
        mean_w = vals[matched["win"] == 1].mean()
        mean_l = vals[matched["win"] == 0].mean()
        print(f"  {feat:<28} {corr_pnl:>+10.3f} {corr_win:>+10.3f} "
              f"{mean_w:>+10.4f} {mean_l:>+10.4f}")
        importances.append((feat, abs(corr_pnl), corr_pnl, corr_win))

    # 4. CVD divergence as filter
    print("\n── CVD Divergence as Signal Filter ──")
    for lookback in [12, 24]:
        feat = f"cvd_price_diverg_{lookback}"
        vals = results[feat].replace([np.inf, -np.inf], np.nan).dropna()
        matched = results.loc[vals.index]

        # For longs: positive divergence (CVD up, price flat) = accumulation
        longs = matched[matched["direction"] == "long"]
        shorts = matched[matched["direction"] == "short"]

        if len(longs) >= 10:
            median = longs[feat].median()
            aligned = longs[
                ((longs["direction"] == "long") & (longs[feat] > median))
            ]
            misaligned = longs[
                ((longs["direction"] == "long") & (longs[feat] <= median))
            ]
            print(f"\n  LONG + {feat}:")
            print(f"    CVD aligned (>{median:.3f}):    N={len(aligned):>3}, "
                  f"WR={aligned['win'].mean():.1%}, "
                  f"avg PnL={aligned['pnl'].mean():+.3f}%")
            print(f"    CVD misaligned (<={median:.3f}): N={len(misaligned):>3}, "
                  f"WR={misaligned['win'].mean():.1%}, "
                  f"avg PnL={misaligned['pnl'].mean():+.3f}%")

        if len(shorts) >= 10:
            median = shorts[feat].median()
            aligned = shorts[shorts[feat] < median]
            misaligned = shorts[shorts[feat] >= median]
            print(f"\n  SHORT + {feat}:")
            print(f"    CVD aligned (<{median:.3f}):     N={len(aligned):>3}, "
                  f"WR={aligned['win'].mean():.1%}, "
                  f"avg PnL={aligned['pnl'].mean():+.3f}%")
            print(f"    CVD misaligned (>={median:.3f}): N={len(misaligned):>3}, "
                  f"WR={misaligned['win'].mean():.1%}, "
                  f"avg PnL={misaligned['pnl'].mean():+.3f}%")

    # 5. ML: GradientBoosting with CVD features
    print("\n── ML: GradientBoosting with CVD features ──")
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_score

        feature_cols = CVD_FEATURES
        X = results[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0)
        y = results["win"]

        if len(X) >= 50:
            # CVD-only model
            clf = GradientBoostingClassifier(
                n_estimators=100, max_depth=3, random_state=42
            )
            scores = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
            print(f"  CVD-only accuracy: {scores.mean():.3f} ± {scores.std():.3f}")
            print(f"  Baseline (always majority): {max(y.mean(), 1 - y.mean()):.3f}")

            # Feature importance
            clf.fit(X, y)
            fi = pd.Series(clf.feature_importances_, index=feature_cols)
            fi = fi.sort_values(ascending=False)
            print(f"\n  Feature importances:")
            for fname, imp in fi.items():
                print(f"    {fname:<30} {imp:.3f}")
        else:
            print(f"  Too few samples ({len(X)}) for ML")

    except ImportError:
        print("  sklearn not installed, skipping ML test")

    # 6. Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    importances.sort(key=lambda x: x[1], reverse=True)
    if importances:
        best = importances[0]
        print(f"Best feature: {best[0]} (|corr|={best[1]:.3f})")
        if best[1] > 0.05:
            print("→ CVD has SOME signal. Worth adding to Miro ML pipeline.")
        else:
            print("→ CVD features show NO meaningful correlation.")
            print("  Confirms TIB finding: buy/sell classification = noise in crypto,")
            print("  even at 4H aggregation level.")


if __name__ == "__main__":
    main()
