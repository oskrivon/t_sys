"""Score cached range trades with ML filter + Vision.

Loads pre-computed trades from pickle, applies ML filter, scores with Vision.
"""
from __future__ import annotations

import asyncio
import os
import sys
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
load_dotenv()

from src.ai.chart_generator import generate_chart
from src.ai.vision_scorer import VisionScorer
from src.backtest.metrics import compute_metrics
from src.strategy.levels import get_rolling_levels

VISION_PROMPT = """You are an expert crypto range trader evaluating chart setups.

This is a {tf} candlestick chart for {symbol}.
- Green horizontal lines = support levels
- Red horizontal lines = resistance levels
- The blue vertical line marks the entry point
- Arrow shows entry direction ({direction})

This is a RANGE TRADE (mean reversion):
- LONG entries are at support, expecting bounce UP to resistance
- SHORT entries are at resistance, expecting bounce DOWN to support

Evaluate this range trade setup on a scale 1-10:
- 1-3: Bad (no clear range, trending strongly, level broken multiple times)
- 4-5: Mediocre (range exists but messy, wide zones, unclear boundaries)
- 6-7: Good (defined range, clean levels, price respecting boundaries)
- 8-10: Excellent (textbook range, multiple clean bounces, tight levels)

Respond ONLY with JSON: {{"score": N, "reason": "1-2 sentences"}}"""


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set")
        return

    # Load cached trades
    cache_path = Path("data/cache/range_trades.pkl")
    print(f"Loading cached trades from {cache_path}...")
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)

    train_trades = cache["train"]
    test_trades = cache["test"]
    test_data = cache["test_data"]
    print(f"  TRAIN: {len(train_trades)}, TEST: {len(test_trades)}")

    # ML filter
    from sklearn.ensemble import GradientBoostingClassifier

    feature_cols = [
        "corridor_pct", "sup_touches", "sup_score", "res_touches", "res_score",
        "rsi", "atr_pct", "vol_ratio", "dist_to_sup_pct", "dist_to_res_pct",
        "sma20_dist", "sma50_dist", "price_change_7d", "combined_score",
        "combined_touches", "rr",
    ]

    def trades_to_xy(trades):
        X, y = [], []
        valid_indices = []
        for i, t in enumerate(trades):
            meta = t.metadata or {}
            row = [meta.get(f, 0) for f in feature_cols]
            if any(pd.isna(v) for v in row):
                continue
            X.append(row)
            y.append(1 if t.net_pnl_pct > 0 else 0)
            valid_indices.append(i)
        return np.array(X), np.array(y), valid_indices

    X_train, y_train, _ = trades_to_xy(train_trades)
    X_test, y_test, test_valid = trades_to_xy(test_trades)

    print(f"Training ML on {len(X_train)} trades...")
    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, min_samples_leaf=5,
        learning_rate=0.1, random_state=42,
    )
    clf.fit(X_train, y_train)

    test_proba = clf.predict_proba(X_test)[:, 1]
    ml_mask = test_proba >= 0.3
    ml_indices = [test_valid[i] for i, m in enumerate(ml_mask) if m]
    ml_trades = [test_trades[i] for i in ml_indices]
    ml_probas = test_proba[ml_mask]

    n_win = sum(1 for t in ml_trades if t.net_pnl_pct > 0)
    print(f"ML filter (P>=0.3): {len(ml_trades)} trades, WR {n_win/max(1,len(ml_trades)):.1%}")
    print(f"Estimated Vision cost: ${len(ml_trades) * 0.01:.2f}")

    # Score with Vision
    import src.ai.vision_scorer as vs
    vs.PROMPT_TEMPLATE = VISION_PROMPT
    scorer = VisionScorer(api_key=api_key)

    results = []
    for i, trade in enumerate(ml_trades):
        symbol = trade.symbol
        meta = trade.metadata or {}
        df = test_data.get(symbol)
        if df is None:
            continue

        # Find entry index
        diffs = (df["ts"] - trade.entry_time).abs()
        entry_idx = int(diffs.idxmin())

        # Get levels
        levels = get_rolling_levels(df, entry_idx, lookback=200, min_touches=2,
                                     tolerance_pct=1.0, min_level_age=10, swing_order=5)
        level_prices = [lv.price for lv in levels]

        # Generate chart
        chart_png = generate_chart(
            df, entry_idx, level_prices,
            signal_type=meta.get("type", "range"),
            is_long=(trade.side.value == "long"),
            candles_before=60, candles_after=5,
        )
        if not chart_png:
            continue

        # Score
        result = await scorer.score(
            chart_png,
            symbol.replace("USDT", "/USDT"),
            signal_type=meta.get("type", "range"),
            is_long=(trade.side.value == "long"),
            timeframe="4h",
        )

        if result and "score" in result:
            score = result["score"]
            win = 1 if trade.net_pnl_pct > 0 else 0
            results.append({
                "symbol": symbol, "side": trade.side.value,
                "score": score, "reason": result.get("reason", ""),
                "win": win, "net_pnl_pct": trade.net_pnl_pct,
                "ml_proba": float(ml_probas[i]),
                "rr": meta.get("rr", 0),
                "corridor_pct": meta.get("corridor_pct", 0),
            })
            status = "W" if win else "L"
            print(f"  [{i+1}/{len(ml_trades)}] {symbol:12s} {trade.side.value:5s} "
                  f"s={score} {status} pnl={trade.net_pnl_pct:+.3f}% "
                  f"| {result.get('reason', '')[:50]}")
        else:
            print(f"  [{i+1}/{len(ml_trades)}] {symbol:12s} FAILED")

        # Small delay to avoid rate limiting
        await asyncio.sleep(0.5)

    # Analyze
    if not results:
        print("\nNo results")
        return

    df_res = pd.DataFrame(results)
    print(f"\n{'='*70}")
    print(f"  RESULTS: {len(df_res)} trades scored")
    print(f"{'='*70}")
    print(f"  Overall WR: {df_res['win'].mean():.1%}")

    print(f"\n  By Vision score bucket:")
    for lo, hi, label in [(1,3,"1-3"), (4,5,"4-5"), (6,7,"6-7"), (8,10,"8-10")]:
        b = df_res[(df_res["score"] >= lo) & (df_res["score"] <= hi)]
        if len(b) == 0:
            print(f"    {label}: 0 trades")
            continue
        print(f"    {label}: {len(b):3d}t, WR {b['win'].mean():.1%}, "
              f"avg_pnl {b['net_pnl_pct'].mean():+.4f}%")

    for thr in [6, 7, 8]:
        f = df_res[df_res["score"] >= thr]
        if len(f) >= 3:
            ft = [ml_trades[i] for i in f.index if i < len(ml_trades)]
            if ft:
                m = compute_metrics(ft)
                print(f"\n  Score >= {thr}: {len(f)}t, WR {f['win'].mean():.1%}, "
                      f"PF {m.profit_factor:.2f}, exp {m.expectancy_pct:+.3f}%")

    # Save
    df_res.to_csv("data/reports/range_vision_results.csv", index=False)
    print(f"\n  Saved to data/reports/range_vision_results.csv")


if __name__ == "__main__":
    asyncio.run(main())
