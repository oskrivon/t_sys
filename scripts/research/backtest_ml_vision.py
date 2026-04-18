"""
Backtest with ML filter + Vision scoring on 1H and 4H.

1. Collect trades with features (walk-forward)
2. Train ML per-TF, apply threshold filtering
3. Run Vision on sample to check 1H correlation

Usage:
    python scripts/research/backtest_ml_vision.py
    python scripts/research/backtest_ml_vision.py --vision 50   # also run vision on 50 samples
"""
from __future__ import annotations

import argparse
import sys
import time
import os
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import ccxt

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10
INITIAL_CAPITAL = 10_000


@dataclass
class Config:
    name: str
    tf: str
    months: int
    level_lookback: int
    level_min_touches: int
    level_tolerance_pct: float
    retest_window: int
    min_level_age: int
    trend_sma: int
    rr_ratio: float
    max_risk_pct: float
    check_interval: int
    max_hold: int


CONFIGS = [
    Config("1H_standard", "1h", 4, 200, 2, 1.0, 15, 20, 50, 3.0, 3.0, 4, 150),
    Config("1H_scalp", "1h", 4, 72, 2, 0.5, 6, 6, 20, 2.0, 2.0, 2, 24),
    Config("4H_baseline", "4h", 8, 200, 2, 1.0, 15, 20, 50, 3.0, 3.0, 6, 150),
]


def fetch_ohlcv(symbol: str, tf: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c = []
    cur = since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except Exception:
            break
        if not c:
            break
        all_c.extend(c)
        last = c[-1][0]
        if last <= cur or len(c) < 1000:
            break
        cur = last + 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def load_data(tf: str, months: int) -> dict[str, pd.DataFrame]:
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{tf}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            datasets[symbol] = df
            continue
        sys.stdout.write(f"  Downloading {symbol} {tf}...")
        sys.stdout.flush()
        df = fetch_ohlcv(symbol, tf, months)
        if not df.empty:
            df.to_parquet(path, index=False)
            sys.stdout.write(f" {len(df)} candles\n")
            datasets[symbol] = df
        else:
            sys.stdout.write(" FAILED\n")
    return datasets


def collect_trades(datasets: dict[str, pd.DataFrame], cfg: Config) -> pd.DataFrame:
    """Walk-forward, collect trades WITH features."""
    all_records = []

    for symbol, df in datasets.items():
        sma = df["close"].rolling(cfg.trend_sma).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts: list[Breakout] = []
        cached_levels = []
        last_check = 0
        warmup = max(cfg.level_lookback, cfg.trend_sma) + 10

        for i in range(warmup, len(df)):
            if active_trade is not None:
                h, l = df["high"].iloc[i], df["low"].iloc[i]
                at = active_trade
                outcome, exit_price = None, None

                if at["is_long"]:
                    if l <= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif h >= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > cfg.max_hold: outcome, exit_price = "timeout", df["close"].iloc[i]
                else:
                    if h >= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif l <= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > cfg.max_hold: outcome, exit_price = "timeout", df["close"].iloc[i]

                if outcome:
                    ep = at["entry_price"]
                    pnl_pct = ((exit_price - ep) / ep if at["is_long"] else (ep - exit_price) / ep) - 2 * FEE_BPS / 10000
                    record = {
                        **at["features"],
                        "outcome": outcome, "pnl_pct": pnl_pct,
                        "symbol": symbol, "signal_type": at["signal_type"],
                        "entry_idx": at["entry_idx"],
                        "is_long": at["is_long"],
                        "hold_candles": i - at["entry_idx"],
                    }
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            if i - last_check >= cfg.check_interval:
                cached_levels = get_rolling_levels(
                    df, i, cfg.level_lookback, cfg.level_min_touches,
                    cfg.level_tolerance_pct, cfg.min_level_age,
                )
                last_check = i
                new_brk = detect_breakouts(df, i, cached_levels)
                recent_breakouts.extend(new_brk)
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= cfg.retest_window]

            close_i = df["close"].iloc[i]
            retest = detect_retests(df, i, recent_breakouts, cfg.retest_window, trend)
            zakol = detect_zakol(df, i, cached_levels, trend)

            best = None
            if retest and zakol:
                best = retest if retest[2] >= zakol[2] else zakol
            else:
                best = retest or zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, cfg.rr_ratio)
                if signal is None:
                    continue

                features = compute_features(df, i, lv, sig_type, cfg.trend_sma)
                sl_dist = abs(close_i - signal.sl)
                risk_amt = INITIAL_CAPITAL * cfg.max_risk_pct / 100
                pos_size = min(risk_amt / (sl_dist / close_i), INITIAL_CAPITAL * 0.90)

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "position_size": pos_size,
                    "signal_type": sig_type.value,
                    "features": features,
                }

    return pd.DataFrame(all_records)


def run_ml_analysis(trades_df: pd.DataFrame, cfg: Config) -> None:
    """Train ML, threshold analysis, return estimation."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_predict

    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    df["target"] = (df["outcome"] == "win").astype(int)

    feature_cols = [c for c in df.columns
                    if c not in ["outcome", "target", "pnl_pct", "symbol", "signal_type",
                                 "entry_idx", "is_long", "hold_candles"]]

    X = df[feature_cols].fillna(0).values
    y = df["target"].values

    print(f"\n  ML: {len(df)} trades ({y.sum()} wins, {len(y)-y.sum()} losses), baseline WR {y.mean()*100:.1f}%")

    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        min_samples_leaf=10, subsample=0.8, random_state=42,
    )
    y_prob = cross_val_predict(clf, X, y, cv=5, method="predict_proba")[:, 1]

    # Hours per candle
    hpc = 1 if cfg.tf == "1h" else 4
    period_months = cfg.months * 0.85  # approx usable period after warmup

    print(f"\n  {'Threshold':>10} {'Trades':>7} {'T/mo':>6} {'WR':>6} {'Exp':>9} {'Monthly':>8} {'Annual':>8} {'Hold(h)':>8}")
    for thr in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        mask = y_prob >= thr
        if mask.sum() < 10:
            continue
        fy = y[mask]
        fp = df["pnl_pct"].values[mask]
        fh = df["hold_candles"].values[mask]
        wr = fy.mean()
        aw = fp[fy == 1].mean() if (fy == 1).sum() > 0 else 0
        al = abs(fp[fy == 0].mean()) if (fy == 0).sum() > 0 else 0
        exp = wr * aw - (1 - wr) * al
        tpm = mask.sum() / period_months
        monthly = tpm * exp * cfg.max_risk_pct / 100
        annual = (1 + monthly) ** 12 - 1
        avg_h = fh.mean() * hpc

        print(f"  {thr:>10.2f} {mask.sum():>7} {tpm:>5.0f} {wr*100:>5.1f}% {exp*100:>+8.3f}% {monthly*100:>+7.2f}% {annual*100:>+7.1f}% {avg_h:>7.0f}")

    # Feature importance
    clf.fit(X, y)
    fi = sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 10 features:")
    for name, imp in fi[:10]:
        print(f"    {name:>25s}: {imp:.4f}")

    # Best per-symbol at threshold 0.35
    mask35 = y_prob >= 0.35
    if mask35.sum() > 10:
        print(f"\n  Per-symbol at threshold 0.35:")
        fs = df["symbol"].values[mask35]
        fy = y[mask35]
        for sym in sorted(set(fs)):
            sm = fs == sym
            if sm.sum() > 0:
                sw = fy[sm].mean() * 100
                print(f"    {sym:12s}: {sm.sum():3d} trades, {sw:.0f}% WR")

    return y_prob, clf, feature_cols


def run_vision_sample(trades_df: pd.DataFrame, datasets: dict[str, pd.DataFrame],
                      cfg: Config, n_samples: int = 50) -> None:
    """Run Claude Vision on a sample of trades."""
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        print("\n  Vision: OPENROUTER_API_KEY not set, skipping")
        return

    from src.ai.chart_generator import generate_chart
    import openai
    import json
    import re

    df = trades_df[trades_df["outcome"].isin(["win", "loss"])].copy()
    wins = df[df["outcome"] == "win"]
    losses = df[df["outcome"] == "loss"]

    n_each = n_samples // 2
    sampled = pd.concat([
        wins.sample(min(n_each, len(wins)), random_state=42),
        losses.sample(min(n_each, len(losses)), random_state=42),
    ]).sample(frac=1, random_state=42)

    print(f"\n  Vision: evaluating {len(sampled)} samples (~${len(sampled)*0.004:.2f})")

    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    import base64
    hpc = 1 if cfg.tf == "1h" else 4

    results = []
    for i, (_, row) in enumerate(sampled.iterrows()):
        symbol = row["symbol"]
        entry_idx = int(row["entry_idx"])
        is_long = bool(row["is_long"])
        sig_type = row["signal_type"]
        outcome = row["outcome"]

        dfs = datasets[symbol]
        # Get level prices from nearby levels
        levels = get_rolling_levels(dfs, entry_idx, cfg.level_lookback,
                                     cfg.level_min_touches, cfg.level_tolerance_pct,
                                     cfg.min_level_age)
        level_prices = [lv.price for lv in levels[:8]]

        chart_bytes = generate_chart(
            dfs, entry_idx, level_prices, sig_type, is_long,
            candles_before=60, candles_after=0,
        )
        if not chart_bytes:
            continue

        image_b64 = base64.b64encode(chart_bytes).decode()
        direction = "LONG (buy)" if is_long else "SHORT (sell)"
        prompt = f"""You are an expert crypto trader evaluating chart setups.

This is a {cfg.tf} candlestick chart for {symbol}.
- Green horizontal lines = support levels
- Red horizontal lines = resistance levels
- The blue vertical line marks where a {sig_type} signal was detected
- Arrow shows the entry direction ({direction})

Evaluate this setup quality on a scale 1-10:
- 1-3: Bad setup (messy levels, no clear structure, against trend)
- 4-5: Mediocre (level exists but context is weak)
- 6-7: Good (clear level, clean retouches, aligned with structure)
- 8-10: Excellent (textbook pattern, strong level, perfect context)

Respond ONLY with JSON: {{"score": N, "reason": "brief explanation"}}"""

        try:
            response = client.chat.completions.create(
                model="anthropic/claude-sonnet-4.6",
                max_tokens=200,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            text = response.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r'"score"\s*:\s*(\d+)', text)
                result = {"score": int(m.group(1)), "reason": text[:100]} if m else {"score": -1}

            score = result.get("score", -1)
            marker = "W" if outcome == "win" else "L"
            print(f"    [{i+1}/{len(sampled)}] {symbol:10s} [{marker}] score={score:>2}")
            results.append({"outcome": outcome, "score": score, "symbol": symbol})
        except Exception as e:
            print(f"    [{i+1}/{len(sampled)}] {symbol:10s} ERROR: {str(e)[:60]}")

        time.sleep(0.5)

    if not results:
        return

    rdf = pd.DataFrame(results)
    valid = rdf[rdf["score"] > 0]
    if valid.empty:
        return

    ws = valid[valid["outcome"] == "win"]["score"]
    ls = valid[valid["outcome"] == "loss"]["score"]
    corr = valid["score"].corr((valid["outcome"] == "win").astype(int))

    print(f"\n  Vision Results ({cfg.name}):")
    print(f"    Avg score: wins={ws.mean():.1f}, losses={ls.mean():.1f}, delta={ws.mean()-ls.mean():+.1f}")
    print(f"    Correlation: {corr:+.3f}")

    print(f"\n    {'Score >=':>10} {'Trades':>7} {'WR':>6}")
    for thr in range(4, 9):
        above = valid[valid["score"] >= thr]
        if len(above) >= 5:
            wr = (above["outcome"] == "win").mean()
            print(f"    {'>=' + str(thr):>10} {len(above):>7} {wr*100:>5.0f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision", type=int, default=0, help="Vision samples per config (0=skip)")
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    print("=== Miro Strategy: ML + Vision Backtest ===\n")

    datasets_by_tf: dict[str, dict[str, pd.DataFrame]] = {}
    for cfg in CONFIGS:
        if cfg.tf not in datasets_by_tf:
            print(f"Loading {cfg.tf} data...")
            datasets_by_tf[cfg.tf] = load_data(cfg.tf, cfg.months)

    for cfg in CONFIGS:
        print(f"\n{'='*70}")
        print(f"  {cfg.name}: TF={cfg.tf}, R:R=1:{cfg.rr_ratio}, max_hold={cfg.max_hold}")
        print(f"{'='*70}")

        datasets = datasets_by_tf[cfg.tf]

        print(f"\n  Collecting trades with features...")
        trades_df = collect_trades(datasets, cfg)
        print(f"  Total: {len(trades_df)} trades")

        if len(trades_df) < 50:
            print("  Not enough trades!")
            continue

        run_ml_analysis(trades_df, cfg)

        if args.vision > 0:
            run_vision_sample(trades_df, datasets, cfg, args.vision)


if __name__ == "__main__":
    main()
