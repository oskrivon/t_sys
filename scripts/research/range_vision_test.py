"""Range Trading: ML + Vision scoring on TEST set.

Pipeline:
  1. Run range backtest on TRAIN and TEST
  2. Train ML on TRAIN
  3. Filter TEST signals with ML (P>=0.3)
  4. Generate charts for filtered signals
  5. Score with Claude Vision via OpenRouter
  6. Analyze results by score bucket
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

load_dotenv()

from src.ai.chart_generator import generate_chart
from src.ai.vision_scorer import VisionScorer
from src.backtest.presets import bybit_futures
from src.backtest.metrics import compute_metrics
from src.backtest.models import Trade

# Reuse strategy from range backtest
from scripts.research.backtest_range_trading import (
    RangeStrategy, load_data, split_data, SYMBOLS, TIMEFRAME, MAX_HOLD,
    LOOKBACK, SWING_ORDER, MIN_LEVEL_AGE, TOLERANCE_PCT, MIN_TOUCHES,
)
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

Key things to check:
1. Is price actually in a range or trending?
2. How many times has this level been respected?
3. Is there room to reach the opposite side of the range?
4. Are the candles showing rejection (wicks) at the level?

Respond ONLY with JSON: {{"score": N, "reason": "1-2 sentences"}}"""


async def run_vision_test():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set")
        return

    scorer = VisionScorer(api_key=api_key)
    # Override the prompt template for range trading
    import src.ai.vision_scorer as vs
    vs.PROMPT_TEMPLATE = VISION_PROMPT

    # 1. Load data and split
    print("Loading data...")
    data = load_data(SYMBOLS, TIMEFRAME)
    train_data, test_data = split_data(data, frac=0.5)
    print(f"Loaded {len(data)} symbols")

    # 2. Run backtest
    strategy = RangeStrategy()
    cost = bybit_futures()

    print("Running TRAIN backtest...")
    train_trades = strategy.run(train_data, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(train_trades)
    print(f"  TRAIN: {len(train_trades)} trades, WR {sum(1 for t in train_trades if t.net_pnl_pct > 0)/len(train_trades):.1%}")

    print("Running TEST backtest...")
    test_trades = strategy.run(test_data, position_size=1000, max_hold=MAX_HOLD)
    cost.apply_all(test_trades)
    print(f"  TEST: {len(test_trades)} trades, WR {sum(1 for t in test_trades if t.net_pnl_pct > 0)/len(test_trades):.1%}")

    # 3. Train ML and filter TEST
    from sklearn.ensemble import GradientBoostingClassifier

    feature_cols = [
        "corridor_pct", "sup_touches", "sup_score", "res_touches", "res_score",
        "rsi", "atr_pct", "vol_ratio", "dist_to_sup_pct", "dist_to_res_pct",
        "sma20_dist", "sma50_dist", "price_change_7d", "combined_score",
        "combined_touches", "rr",
    ]

    def trades_to_xy(trades):
        X, y = [], []
        for t in trades:
            meta = t.metadata or {}
            row = [meta.get(f, 0) for f in feature_cols]
            if any(pd.isna(v) for v in row):
                continue
            X.append(row)
            y.append(1 if t.net_pnl_pct > 0 else 0)
        return np.array(X), np.array(y)

    X_train, y_train = trades_to_xy(train_trades)
    X_test, y_test = trades_to_xy(test_trades)

    print(f"\nTraining ML on {len(X_train)} samples...")
    clf = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, min_samples_leaf=5,
        learning_rate=0.1, random_state=42,
    )
    clf.fit(X_train, y_train)

    test_proba = clf.predict_proba(X_test)[:, 1]
    ml_mask = test_proba >= 0.3  # broader filter to give Vision more to work with
    ml_filtered_indices = [i for i, m in enumerate(ml_mask) if m]
    ml_filtered_trades = [test_trades[i] for i in ml_filtered_indices]
    ml_filtered_proba = test_proba[ml_mask]

    print(f"ML filter (P>=0.3): {len(ml_filtered_trades)} trades pass "
          f"(WR {sum(1 for t in ml_filtered_trades if t.net_pnl_pct > 0)/max(1,len(ml_filtered_trades)):.1%})")

    # 4. Generate charts and score with Vision
    print(f"\nScoring {len(ml_filtered_trades)} trades with Vision...")
    print(f"Estimated cost: ${len(ml_filtered_trades) * 0.01:.2f}")

    results = []
    for i, trade in enumerate(ml_filtered_trades):
        symbol = trade.symbol
        meta = trade.metadata or {}
        entry_idx = meta.get("_entry_idx", None)

        # We need original entry_idx — reconstruct from trade data
        df = test_data.get(symbol)
        if df is None:
            continue

        # Find entry candle by timestamp
        entry_mask = df["ts"] == trade.entry_time
        if not entry_mask.any():
            # Find closest
            diffs = (df["ts"] - trade.entry_time).abs()
            entry_idx = diffs.idxmin()
        else:
            entry_idx = entry_mask.idxmax()

        # Get levels at entry time
        levels = get_rolling_levels(
            df, entry_idx,
            lookback=LOOKBACK, min_touches=MIN_TOUCHES,
            tolerance_pct=TOLERANCE_PCT, min_level_age=MIN_LEVEL_AGE,
            swing_order=SWING_ORDER,
        )
        level_prices = [lv.price for lv in levels]

        # Generate chart
        chart_png = generate_chart(
            df, entry_idx, level_prices,
            signal_type=meta.get("type", "range"),
            is_long=(trade.side.value == "long"),
            candles_before=60,
            candles_after=5,
        )
        if not chart_png:
            continue

        # Score with Vision
        sym_display = symbol.replace("USDT", "/USDT")
        direction = "LONG (buy)" if trade.side.value == "long" else "SHORT (sell)"
        result = await scorer.score(
            chart_png, sym_display,
            signal_type=meta.get("type", "range"),
            is_long=(trade.side.value == "long"),
            timeframe=TIMEFRAME,
        )

        if result and "score" in result:
            score = result["score"]
            win = 1 if trade.net_pnl_pct > 0 else 0
            results.append({
                "symbol": symbol,
                "side": trade.side.value,
                "score": score,
                "reason": result.get("reason", ""),
                "win": win,
                "net_pnl_pct": trade.net_pnl_pct,
                "ml_proba": float(ml_filtered_proba[i]),
                "rr": meta.get("rr", 0),
                "corridor_pct": meta.get("corridor_pct", 0),
            })
            status = "WIN" if win else "LOSS"
            print(f"  [{i+1}/{len(ml_filtered_trades)}] {sym_display:12s} "
                  f"{trade.side.value:5s} score={score} {status} "
                  f"pnl={trade.net_pnl_pct:+.3f}% | {result.get('reason', '')[:60]}")
        else:
            print(f"  [{i+1}/{len(ml_filtered_trades)}] {sym_display:12s} FAILED")

    # 5. Analyze results
    if not results:
        print("\nNo results to analyze")
        return

    df_res = pd.DataFrame(results)
    print(f"\n{'='*70}")
    print(f"  Vision Scoring Results: {len(df_res)} trades scored")
    print(f"{'='*70}")
    print(f"  Overall WR: {df_res['win'].mean():.1%}")
    print(f"  Score distribution: {df_res['score'].describe().to_dict()}")

    print(f"\n  By Vision score:")
    for lo, hi, label in [(1,3,"1-3"), (4,5,"4-5"), (6,7,"6-7"), (8,10,"8-10")]:
        bucket = df_res[(df_res["score"] >= lo) & (df_res["score"] <= hi)]
        if len(bucket) == 0:
            print(f"    Score {label}: 0 trades")
            continue
        wr = bucket["win"].mean()
        avg_pnl = bucket["net_pnl_pct"].mean()
        n = len(bucket)
        print(f"    Score {label}: {n:3d} trades, WR {wr:.1%}, avg_pnl {avg_pnl:+.4f}%")

    # Score >= 7 and >= 8 detailed
    for threshold in [7, 8]:
        filtered = df_res[df_res["score"] >= threshold]
        if len(filtered) >= 3:
            trades_f = [ml_filtered_trades[i] for i in filtered.index]
            m = compute_metrics(trades_f)
            print(f"\n  Score >= {threshold}: {len(filtered)} trades, "
                  f"WR {filtered['win'].mean():.1%}, "
                  f"PF {m.profit_factor:.2f}, exp {m.expectancy_pct:+.3f}%")

    # Long vs Short
    for side in ["long", "short"]:
        sub = df_res[df_res["side"] == side]
        if len(sub) >= 5:
            print(f"\n  {side.upper()} by score:")
            for lo, hi, label in [(1,5,"1-5"), (6,7,"6-7"), (8,10,"8-10")]:
                bucket = sub[(sub["score"] >= lo) & (sub["score"] <= hi)]
                if len(bucket) > 0:
                    print(f"    Score {label}: {len(bucket):3d}t, WR {bucket['win'].mean():.1%}")

    # Save results
    out_path = Path("data/reports/range_vision_results.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_res.to_csv(out_path, index=False)
    print(f"\n  Results saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(run_vision_test())
