"""
Compare Vision models and prompts for setup evaluation.

Tests multiple models and prompt strategies on the same set of trades.
Goal: find which model/prompt gives best win/loss discrimination.

Usage:
    python scripts/research/vision_model_compare.py --n 40
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import openai

from dotenv import load_dotenv
load_dotenv()

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.models import Breakout
from src.ai.chart_generator import generate_chart

API_KEY = os.getenv("OPENROUTER_API_KEY", "")

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "DOGE/USDT", "AVAX/USDT", "NEAR/USDT", "SUI/USDT",
]

# Models to test
MODELS = [
    ("anthropic/claude-sonnet-4.6", "sonnet-4.6"),
    ("anthropic/claude-haiku-4.5", "haiku-4.5"),
    ("anthropic/claude-3.5-sonnet", "sonnet-3.5"),
    ("openai/gpt-4o", "gpt-4o"),
]

# Prompts to test
PROMPT_SIMPLE = """You are an expert crypto trader. Rate this {tf} chart setup for {symbol} from 1-10.
Blue line = entry point. Arrow = direction ({direction}). Green/red lines = S/R levels.
1-3: Bad. 4-5: Weak. 6-7: Good. 8-10: Excellent.
Respond ONLY with JSON: {{"score": N, "reason": "why"}}"""

PROMPT_DETAILED = """You are a professional crypto trader specializing in support/resistance breakout-retest patterns.

Analyze this {tf} candlestick chart for {symbol}:
- Green dashed lines = support levels
- Red dashed lines = resistance levels
- Blue vertical line = entry point ({signal_type} signal detected)
- Arrow shows trade direction: {direction}

Score this setup 1-10 based on:

**Level Quality (40% weight):**
- How many times was the level touched/respected?
- Is the level clearly visible and well-defined?
- Is it a round number or major psychological level?

**Price Action Context (30% weight):**
- Is there a clean breakout followed by retest?
- What do the candle bodies/wicks tell us at the level?
- Is there a clear trend structure (HH/HL for longs, LH/LL for shorts)?

**Risk/Reward (30% weight):**
- Is the entry near the level (tight stop loss)?
- Is there room to the next level for take profit?
- Would you personally take this trade?

Scoring:
1-3: Messy chart, no clear level, counter-trend, skip
4-5: Level exists but entry is sloppy or context is mixed
6-7: Clean level, good retest, trend-aligned, worth considering
8-10: Textbook setup, multiple confirmations, high conviction

Respond ONLY with JSON: {{"score": N, "reason": "2-3 sentences explaining key factors"}}"""

PROMPT_BINARY = """You are a crypto trader. Look at this {tf} chart for {symbol}.
The blue line marks a potential {direction} entry at a support/resistance level.

Simple question: Would you take this trade?

Respond with JSON:
{{"take": true/false, "confidence": 1-10, "reason": "one sentence"}}"""

PROMPTS = [
    ("simple", PROMPT_SIMPLE),
    ("detailed", PROMPT_DETAILED),
    ("binary", PROMPT_BINARY),
]


# -- Config for 4H (where vision previously worked) --
LEVEL_LOOKBACK = 200
LEVEL_MIN_TOUCHES = 2
LEVEL_TOLERANCE_PCT = 1.0
RETEST_WINDOW = 15
MIN_LEVEL_AGE = 20
TREND_SMA = 50
RR_RATIO = 3.0
CHECK_INTERVAL = 6
MAX_HOLD = 150
TF = "4h"


def load_data() -> dict[str, pd.DataFrame]:
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{TF}.parquet"
        if path.exists():
            datasets[symbol] = pd.read_parquet(path)
    return datasets


def collect_trades(datasets):
    """Collect trades for vision testing."""
    all_trades = []
    for symbol, df in datasets.items():
        sma = df["close"].rolling(TREND_SMA).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts = []
        cached_levels = []
        last_check = 0
        warmup = max(LEVEL_LOOKBACK, TREND_SMA) + 10

        for i in range(warmup, len(df)):
            if active_trade is not None:
                h, l = df["high"].iloc[i], df["low"].iloc[i]
                at = active_trade
                outcome = None
                if at["is_long"]:
                    if l <= at["sl"]: outcome = "loss"
                    elif h >= at["tp"]: outcome = "win"
                    elif i - at["entry_idx"] > MAX_HOLD: outcome = "timeout"
                else:
                    if h >= at["sl"]: outcome = "loss"
                    elif l <= at["tp"]: outcome = "win"
                    elif i - at["entry_idx"] > MAX_HOLD: outcome = "timeout"
                if outcome:
                    at["outcome"] = outcome
                    all_trades.append(at)
                    active_trade = None
                else:
                    continue

            if i - last_check >= CHECK_INTERVAL:
                cached_levels = get_rolling_levels(
                    df, i, LEVEL_LOOKBACK, LEVEL_MIN_TOUCHES,
                    LEVEL_TOLERANCE_PCT, MIN_LEVEL_AGE,
                )
                last_check = i
                new_brk = detect_breakouts(df, i, cached_levels)
                recent_breakouts.extend(new_brk)
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= RETEST_WINDOW]

            close_i = df["close"].iloc[i]
            retest = detect_retests(df, i, recent_breakouts, RETEST_WINDOW, trend)
            zakol = detect_zakol(df, i, cached_levels, trend)
            best = retest if retest and (not zakol or retest[2] >= zakol[2]) else zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, RR_RATIO)
                if signal is None:
                    continue
                level_prices = [l.price for l in cached_levels[:10]]
                active_trade = {
                    "symbol": symbol, "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp, "is_long": signal.is_long,
                    "signal_type": sig_type.value, "level_prices": level_prices,
                }

    return all_trades


def call_vision(client, model, prompt, image_b64):
    """Call vision API and parse response."""
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=300,
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
            # Handle binary prompt
            if "take" in result and "score" not in result:
                result["score"] = result.get("confidence", 5)
                if not result.get("take", False):
                    result["score"] = max(1, result["score"] - 3)
            return result
        except json.JSONDecodeError:
            m = re.search(r'"score"\s*:\s*(\d+)', text)
            if m:
                return {"score": int(m.group(1)), "reason": text[:100]}
            m = re.search(r'"confidence"\s*:\s*(\d+)', text)
            if m:
                return {"score": int(m.group(1)), "reason": text[:100]}
            return None
    except Exception as e:
        print(f"      API error: {str(e)[:80]}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40, help="Samples per test")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set")
        return

    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Vision Model & Prompt Comparison ===\n")
    datasets = load_data()

    print("Collecting trades...")
    trades = collect_trades(datasets)
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    print(f"Total: {len(trades)} ({len(wins)} wins, {len(losses)} losses)")

    # Balanced sample
    n_each = args.n // 2
    np.random.seed(42)
    sampled_wins = [wins[i] for i in np.random.choice(len(wins), min(n_each, len(wins)), replace=False)]
    sampled_losses = [losses[i] for i in np.random.choice(len(losses), min(n_each, len(losses)), replace=False)]
    samples = sampled_wins + sampled_losses
    np.random.shuffle(samples)

    # Pre-generate charts (same charts for all models)
    print(f"\nGenerating {len(samples)} charts...")
    charts = []
    for trade in samples:
        df = datasets[trade["symbol"]]
        chart_bytes = generate_chart(
            df, trade["entry_idx"], trade.get("level_prices", []),
            trade["signal_type"], trade["is_long"],
            candles_before=60, candles_after=0,
        )
        charts.append(base64.b64encode(chart_bytes).decode() if chart_bytes else "")

    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)

    # Test each model × prompt combination
    all_results = []

    for model_id, model_name in MODELS:
        for prompt_name, prompt_template in PROMPTS:
            combo = f"{model_name}/{prompt_name}"
            print(f"\n  Testing {combo}...")

            scores = []
            for i, (trade, chart_b64) in enumerate(zip(samples, charts)):
                if not chart_b64:
                    continue

                direction = "LONG (buy)" if trade["is_long"] else "SHORT (sell)"
                prompt = prompt_template.format(
                    tf=TF, symbol=trade["symbol"],
                    signal_type=trade["signal_type"], direction=direction,
                )

                result = call_vision(client, model_id, prompt, chart_b64)
                if result and "score" in result and result["score"] > 0:
                    scores.append({
                        "outcome": trade["outcome"],
                        "score": result["score"],
                    })

                # Progress
                if (i + 1) % 10 == 0:
                    sys.stdout.write(f"    {i+1}/{len(samples)} ")
                    sys.stdout.flush()

                time.sleep(0.3)

            if not scores:
                print(f"    No valid scores!")
                continue

            sdf = pd.DataFrame(scores)
            valid = sdf[sdf["score"] > 0]
            ws = valid[valid["outcome"] == "win"]["score"]
            ls = valid[valid["outcome"] == "loss"]["score"]
            corr = valid["score"].corr((valid["outcome"] == "win").astype(int))

            # Threshold analysis
            best_wr = 0
            best_thr = 0
            thr_info = []
            for thr in range(5, 9):
                above = valid[valid["score"] >= thr]
                if len(above) >= 5:
                    wr = (above["outcome"] == "win").mean()
                    thr_info.append(f">={thr}:{wr*100:.0f}%({len(above)})")
                    if wr > best_wr:
                        best_wr = wr
                        best_thr = thr

            thresholds = " | ".join(thr_info) if thr_info else "n/a"

            print(f"\n    {combo}: n={len(valid)}, "
                  f"W={ws.mean():.1f} L={ls.mean():.1f} D={ws.mean()-ls.mean():+.1f}, "
                  f"corr={corr:+.3f}")
            print(f"    Thresholds: {thresholds}")

            all_results.append({
                "combo": combo,
                "model": model_name,
                "prompt": prompt_name,
                "n": len(valid),
                "avg_win": ws.mean(),
                "avg_loss": ls.mean(),
                "delta": ws.mean() - ls.mean(),
                "corr": corr,
                "best_wr": best_wr,
                "best_thr": best_thr,
            })

    # Summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  {'Combo':<30} {'N':>4} {'D':>6} {'Corr':>7} {'Best WR':>8}")
    for r in sorted(all_results, key=lambda x: x["corr"], reverse=True):
        bwr = f"{r['best_wr']*100:.0f}%@{r['best_thr']}" if r['best_wr'] > 0 else "n/a"
        print(f"  {r['combo']:<30} {r['n']:>4} {r['delta']:>+5.1f} {r['corr']:>+6.3f} {bwr:>8}")


if __name__ == "__main__":
    main()
