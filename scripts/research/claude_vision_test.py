"""
Claude Vision API test: оценка качества торговых сетапов по скриншоту графика.

Берём trades из v3 backtest (wins и losses), генерируем графики с уровнями,
отправляем в Claude Sonnet через OpenRouter, получаем оценку 1-10.
Проверяем: коррелирует ли оценка с реальным outcome.

Пример:
    python scripts/research/claude_vision_test.py --n 20
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
import json
from pathlib import Path

import pandas as pd
import numpy as np

DATA = Path("data/processed/candles")
REPORTS = Path("data/reports")

# Load API key
from dotenv import load_dotenv
load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY", "")

PROMPT_TEMPLATE = """You are an expert crypto trader evaluating chart setups.

This is a {tf} candlestick chart for {symbol}.
- Green horizontal lines = support levels
- Red horizontal lines = resistance levels
- The blue vertical line marks where a {signal_type} signal was detected
- Arrow shows the entry direction ({direction})

Evaluate this setup quality on a scale 1-10:
- 1-3: Bad setup (messy levels, no clear structure, against trend)
- 4-5: Mediocre (level exists but context is weak)
- 6-7: Good (clear level, clean retouches, aligned with structure)
- 8-10: Excellent (textbook pattern, strong level, perfect context)

Respond ONLY with JSON: {{"score": N, "reason": "brief explanation in 1-2 sentences"}}"""


def generate_chart(df: pd.DataFrame, entry_idx: int, levels: list[float],
                   signal_type: str, is_long: bool) -> str:
    """Generate candlestick chart with levels as base64 PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    # Show 80 candles around entry (60 before, 20 after)
    start = max(0, entry_idx - 60)
    end = min(len(df), entry_idx + 20)
    chunk = df.iloc[start:end].copy()

    if len(chunk) < 10:
        return ""

    fig, ax = plt.subplots(figsize=(12, 6), facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    # Plot candlesticks manually
    for i, (_, row) in enumerate(chunk.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = "#26a69a" if c >= o else "#ef5350"
        # Body
        body_bottom = min(o, c)
        body_height = abs(c - o)
        ax.bar(i, body_height, bottom=body_bottom, width=0.6, color=color, edgecolor=color)
        # Wicks
        ax.plot([i, i], [l, body_bottom], color=color, linewidth=0.8)
        ax.plot([i, i], [body_bottom + body_height, h], color=color, linewidth=0.8)

    # Draw levels
    price_range = chunk["high"].max() - chunk["low"].min()
    for lvl in levels:
        if chunk["low"].min() - price_range * 0.1 < lvl < chunk["high"].max() + price_range * 0.1:
            # Determine if support or resistance relative to entry
            entry_price = df["close"].iloc[entry_idx]
            color = "#4caf50" if lvl < entry_price else "#f44336"
            ax.axhline(y=lvl, color=color, linewidth=1, alpha=0.7, linestyle="--")

    # Mark entry point
    entry_local = entry_idx - start
    if 0 <= entry_local < len(chunk):
        ax.axvline(x=entry_local, color="#2196f3", linewidth=1.5, alpha=0.6, linestyle=":")
        # Arrow
        entry_price = df["close"].iloc[entry_idx]
        arrow_color = "#26a69a" if is_long else "#ef5350"
        arrow_dir = 1 if is_long else -1
        ax.annotate(
            "LONG" if is_long else "SHORT",
            xy=(entry_local, entry_price),
            xytext=(entry_local + 3, entry_price + arrow_dir * price_range * 0.08),
            color=arrow_color, fontsize=10, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=arrow_color, lw=2),
        )

    # Styling
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#444")
    ax.spines["left"].set_color("#444")
    ax.tick_params(colors="#888")
    ax.yaxis.label.set_color("#888")
    ax.set_ylabel("Price", color="#888")

    # X labels: show dates for some ticks
    n_labels = min(8, len(chunk))
    step = len(chunk) // n_labels
    tick_positions = list(range(0, len(chunk), step))
    tick_labels = [chunk["ts"].iloc[i].strftime("%m/%d") if hasattr(chunk["ts"].iloc[i], "strftime")
                   else str(i) for i in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, color="#888", fontsize=8)

    plt.tight_layout()

    # Convert to base64
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def call_claude_vision(image_b64: str, symbol: str, signal_type: str,
                       is_long: bool, tf: str = "4h") -> dict:
    """Call Claude Sonnet via OpenRouter with image."""
    import openai

    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY,
    )

    direction = "LONG (buy)" if is_long else "SHORT (sell)"
    prompt = PROMPT_TEMPLATE.format(
        tf=tf, symbol=symbol, signal_type=signal_type, direction=direction,
    )

    try:
        response = client.chat.completions.create(
            model="anthropic/claude-sonnet-4.6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = response.choices[0].message.content.strip()
        # Parse JSON
        # Handle markdown-wrapped JSON
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return result
    except json.JSONDecodeError:
        # Try to extract score from text
        import re
        m = re.search(r'"score"\s*:\s*(\d+)', text)
        if m:
            return {"score": int(m.group(1)), "reason": text}
        return {"score": -1, "reason": f"Parse error: {text[:200]}"}
    except Exception as e:
        return {"score": -1, "reason": str(e)}


def run(n_samples: int = 20) -> None:
    """Run vision test on n_samples trades (balanced wins/losses)."""
    REPORTS.mkdir(parents=True, exist_ok=True)

    if not API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set in .env")
        return

    print(f"=== Claude Vision Setup Evaluator Test ===")
    print(f"Samples: {n_samples} (balanced wins/losses)\n")

    # Load data and collect trades (reuse v3 logic)
    sys.path.insert(0, str(Path(__file__).parent))
    from miro_strategy_v3 import load_data, get_rolling_levels, LEVEL_LOOKBACK, TREND_SMA
    from miro_strategy_v3 import RETEST_WINDOW, MIN_LEVEL_AGE

    datasets = load_data()

    # Collect trades with level info
    print("\nCollecting trades...")
    all_trades = []

    for symbol, df in datasets.items():
        sma = df["close"].rolling(TREND_SMA).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        cached_levels = []
        last_check = 0
        CHECK_INTERVAL = 6
        recent_breakouts = []
        active_trade = None
        warmup = max(LEVEL_LOOKBACK, TREND_SMA) + 10

        for i in range(warmup, len(df)):
            if active_trade is not None:
                h, l = df["high"].iloc[i], df["low"].iloc[i]
                at = active_trade
                outcome = None
                if at["is_long"]:
                    if l <= at["sl"]: outcome = "loss"
                    elif h >= at["tp"]: outcome = "win"
                    elif i - at["entry_idx"] > 150: outcome = "timeout"
                else:
                    if h >= at["sl"]: outcome = "loss"
                    elif l <= at["tp"]: outcome = "win"
                    elif i - at["entry_idx"] > 150: outcome = "timeout"

                if outcome:
                    at["outcome"] = outcome
                    at["symbol"] = symbol
                    at["level_prices"] = [lv["price"] for lv in cached_levels[:10]]
                    all_trades.append(at)
                    active_trade = None
                else:
                    continue

            if i - last_check >= CHECK_INTERVAL:
                cached_levels = get_rolling_levels(df, i)
                last_check = i
                close_i = df["close"].iloc[i]
                prev_close = df["close"].iloc[i - 1]
                for lv in cached_levels:
                    zh, zl = lv["zone_high"], lv["zone_low"]
                    if prev_close <= zh and close_i > zh * 1.001:
                        recent_breakouts.append({"level": lv, "dir": "long", "idx": i})
                    if prev_close >= zl and close_i < zl * 0.999:
                        recent_breakouts.append({"level": lv, "dir": "short", "idx": i})
                recent_breakouts = [b for b in recent_breakouts if i - b["idx"] <= RETEST_WINDOW]

            close_i = df["close"].iloc[i]
            low_i, high_i = df["low"].iloc[i], df["high"].iloc[i]

            best_signal = None
            best_score = 0

            for brk in recent_breakouts:
                lv = brk["level"]
                zh, zl = lv["zone_high"], lv["zone_low"]
                age = i - brk["idx"]
                if age < 1 or age > RETEST_WINDOW: continue
                if brk["dir"] == "long" and low_i <= zh * 1.005 and close_i > zh:
                    if lv["score"] > best_score:
                        best_signal = {"type": "long_retest", "level": lv}
                        best_score = lv["score"]
                if brk["dir"] == "short" and high_i >= zl * 0.995 and close_i < zl:
                    if lv["score"] > best_score:
                        best_signal = {"type": "short_retest", "level": lv}
                        best_score = lv["score"]

            for lv in cached_levels:
                zh, zl = lv["zone_high"], lv["zone_low"]
                if low_i < zl * 0.995 and close_i > zl:
                    if lv["score"] + 1 > best_score:
                        best_signal = {"type": "long_zakol", "level": lv}
                        best_score = lv["score"] + 1
                if high_i > zh * 1.005 and close_i < zh:
                    if lv["score"] + 1 > best_score:
                        best_signal = {"type": "short_zakol", "level": lv}
                        best_score = lv["score"] + 1

            if best_signal:
                lv = best_signal["level"]
                is_long = "long" in best_signal["type"]
                zh, zl = lv["zone_high"], lv["zone_low"]
                zone_w = max(zh - zl, close_i * 0.003)
                if is_long:
                    sl = zl - zone_w * 0.3
                    sl_dist = close_i - sl
                    tp = close_i + sl_dist * 3.0
                else:
                    sl = zh + zone_w * 0.3
                    sl_dist = sl - close_i
                    tp = close_i - sl_dist * 3.0
                if sl_dist <= 0 or sl_dist / close_i > 0.08:
                    continue
                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": sl, "tp": tp, "is_long": is_long,
                    "signal_type": best_signal["type"],
                }

    print(f"Total trades: {len(all_trades)}")
    wins = [t for t in all_trades if t["outcome"] == "win"]
    losses = [t for t in all_trades if t["outcome"] == "loss"]
    print(f"Wins: {len(wins)}, Losses: {len(losses)}")

    # Sample balanced
    n_each = n_samples // 2
    sampled_wins = wins[:n_each] if len(wins) >= n_each else wins
    sampled_losses = losses[:n_each] if len(losses) >= n_each else losses
    samples = sampled_wins + sampled_losses
    np.random.seed(42)
    np.random.shuffle(samples)

    print(f"\nEvaluating {len(samples)} samples via Claude Vision...")
    print(f"Estimated cost: ~${len(samples) * 0.01:.2f}\n")

    results = []
    for idx, trade in enumerate(samples):
        symbol = trade["symbol"]
        df = datasets[symbol]
        entry_idx = trade["entry_idx"]

        # Generate chart
        chart_b64 = generate_chart(
            df, entry_idx,
            trade.get("level_prices", []),
            trade["signal_type"],
            trade["is_long"],
        )

        if not chart_b64:
            print(f"  [{idx+1}/{len(samples)}] {symbol} — chart generation failed")
            continue

        # Call Claude
        result = call_claude_vision(
            chart_b64, symbol, trade["signal_type"], trade["is_long"],
        )

        score = result.get("score", -1)
        reason = result.get("reason", "")
        outcome = trade["outcome"]

        results.append({
            "symbol": symbol,
            "signal_type": trade["signal_type"],
            "is_long": trade["is_long"],
            "outcome": outcome,
            "claude_score": score,
            "reason": reason,
        })

        marker = "W" if outcome == "win" else "L"
        print(f"  [{idx+1}/{len(samples)}] {symbol:12s} {trade['signal_type']:18s} "
              f"[{marker}] score={score:>2} | {reason[:60]}")

        time.sleep(0.5)  # rate limit

    # ── Analysis ──
    rdf = pd.DataFrame(results)
    valid = rdf[rdf["claude_score"] > 0]

    if valid.empty:
        print("\nNo valid results!")
        return

    print(f"\n{'='*60}")
    print("  ANALYSIS")
    print(f"{'='*60}")

    wins_scores = valid[valid["outcome"] == "win"]["claude_score"]
    losses_scores = valid[valid["outcome"] == "loss"]["claude_score"]

    print(f"\n  Average score:")
    print(f"    Wins:   {wins_scores.mean():.1f} (n={len(wins_scores)})")
    print(f"    Losses: {losses_scores.mean():.1f} (n={len(losses_scores)})")
    print(f"    Delta:  {wins_scores.mean() - losses_scores.mean():+.1f}")

    # Threshold analysis
    print(f"\n  Score threshold -> Win rate:")
    for thr in range(3, 9):
        above = valid[valid["claude_score"] >= thr]
        if len(above) >= 3:
            wr = (above["outcome"] == "win").mean()
            print(f"    score >= {thr}: {len(above):3d} trades, WR={wr*100:.0f}%")

    # Correlation
    valid_numeric = valid.copy()
    valid_numeric["win"] = (valid_numeric["outcome"] == "win").astype(int)
    corr = valid_numeric["claude_score"].corr(valid_numeric["win"])
    print(f"\n  Correlation (score vs win): {corr:+.3f}")

    # Write report
    lines = [
        "# Claude Vision Setup Evaluator — Test Results",
        "",
        f"**Model:** Claude Sonnet 4.6 via OpenRouter",
        f"**Samples:** {len(valid)} ({len(wins_scores)} wins, {len(losses_scores)} losses)",
        "",
        f"## Key Metric",
        f"- Avg score WINS: **{wins_scores.mean():.1f}**",
        f"- Avg score LOSSES: **{losses_scores.mean():.1f}**",
        f"- Delta: **{wins_scores.mean() - losses_scores.mean():+.1f}**",
        f"- Correlation: **{corr:+.3f}**",
        "",
        "## Threshold Analysis",
        "",
        "| Min score | Trades | Win rate |",
        "|---:|---:|---:|",
    ]
    for thr in range(3, 9):
        above = valid[valid["claude_score"] >= thr]
        if len(above) >= 3:
            wr = (above["outcome"] == "win").mean()
            lines.append(f"| >= {thr} | {len(above)} | {wr*100:.0f}% |")

    lines += ["", "## Individual Results", "",
              "| Symbol | Type | Dir | Outcome | Score | Reason |",
              "|---|---|---|---|---:|---|"]
    for _, r in valid.iterrows():
        d = "L" if r["is_long"] else "S"
        lines.append(f"| {r['symbol']} | {r['signal_type']} | {d} | {r['outcome']} | "
                     f"{r['claude_score']} | {r['reason'][:80]} |")

    out = REPORTS / "claude_vision_test.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="Number of samples (balanced win/loss)")
    args = ap.parse_args()
    run(args.n)


if __name__ == "__main__":
    cli()
