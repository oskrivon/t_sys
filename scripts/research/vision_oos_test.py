"""
Vision OOS Test: score 200 trades from extended dataset.

Walk-forward: train ML on first 8 months, take 200 test trades,
score each with Claude Vision, measure correlation with outcome.

Cost: ~200 x $0.01 = ~$2
Time: ~200 x 4sec = ~15 min

Usage:
    python scripts/research/vision_oos_test.py [--n 200]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from dotenv import load_dotenv
load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY", "")

from src.strategy.levels import get_rolling_levels, find_swing_points
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

# Use diverse symbols (mix of profitable and unprofitable from walk-forward)
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT",
    "LINK/USDT", "AVAX/USDT", "NEAR/USDT", "FIL/USDT",
    "ARB/USDT", "PEPE/USDT", "INJ/USDT", "SUI/USDT",
    "AAVE/USDT", "COMP/USDT", "DOT/USDT", "HBAR/USDT",
    "APE/USDT", "ALGO/USDT", "TIA/USDT", "WIF/USDT",
]

FEE_BPS = 10


# Chart generation (inline, from claude_vision_test.py)
def generate_chart_b64(df, entry_idx, levels, signal_type, is_long):
    import base64
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    start = max(0, entry_idx - 60)
    end = min(len(df), entry_idx + 20)
    chunk = df.iloc[start:end].copy()
    if len(chunk) < 10:
        return ""

    fig, ax = plt.subplots(figsize=(12, 6), facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    for i, (_, row) in enumerate(chunk.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = "#26a69a" if c >= o else "#ef5350"
        body_bottom = min(o, c)
        body_height = abs(c - o)
        ax.bar(i, body_height, bottom=body_bottom, width=0.6, color=color, edgecolor=color)
        ax.plot([i, i], [l, body_bottom], color=color, linewidth=0.8)
        ax.plot([i, i], [body_bottom + body_height, h], color=color, linewidth=0.8)

    price_range = chunk["high"].max() - chunk["low"].min()
    entry_price = df["close"].iloc[entry_idx]
    for lvl in levels:
        if chunk["low"].min() - price_range * 0.1 < lvl < chunk["high"].max() + price_range * 0.1:
            clr = "#4caf50" if lvl < entry_price else "#f44336"
            ax.axhline(y=lvl, color=clr, linewidth=1, alpha=0.7, linestyle="--")

    entry_local = entry_idx - start
    if 0 <= entry_local < len(chunk):
        ax.axvline(x=entry_local, color="#2196f3", linewidth=1.5, alpha=0.6, linestyle=":")
        arrow_color = "#26a69a" if is_long else "#ef5350"
        arrow_dir = 1 if is_long else -1
        ax.annotate(
            "LONG" if is_long else "SHORT",
            xy=(entry_local, entry_price),
            xytext=(entry_local + 3, entry_price + arrow_dir * price_range * 0.08),
            color=arrow_color, fontsize=10, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=arrow_color, lw=2),
        )

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#444")
    ax.spines["left"].set_color("#444")
    ax.tick_params(colors="#888")
    ax.set_ylabel("Price", color="#888")
    n_labels = min(8, len(chunk))
    step = max(1, len(chunk) // n_labels)
    tick_positions = list(range(0, len(chunk), step))
    tick_labels = [chunk["ts"].iloc[i].strftime("%m/%d") if hasattr(chunk["ts"].iloc[i], "strftime")
                   else str(i) for i in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, color="#888", fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def call_vision(image_b64, symbol, signal_type, is_long, tf="4h"):
    import openai

    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)
    direction = "LONG (buy)" if is_long else "SHORT (sell)"
    prompt = f"""You are an expert crypto trader evaluating chart setups.

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

Respond ONLY with JSON: {{"score": N, "reason": "brief explanation"}}"""

    try:
        response = client.chat.completions.create(
            model="anthropic/claude-sonnet-4",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return result
    except json.JSONDecodeError:
        import re
        m = re.search(r'"score"\s*:\s*(\d+)', text)
        if m:
            return {"score": int(m.group(1)), "reason": text[:200]}
        return {"score": -1, "reason": f"Parse error: {text[:200]}"}
    except Exception as e:
        return {"score": -1, "reason": str(e)}


# Trade collection (reuse from walk-forward)
def load_data(tf):
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{tf}.parquet"
        if path.exists():
            datasets[symbol] = pd.read_parquet(path)
    return datasets


def get_d1_levels_at_time(d1_df, current_ts):
    mask = d1_df["ts"] <= current_ts
    if mask.sum() < 50:
        return []
    d1_subset = d1_df[mask]
    return get_rolling_levels(d1_subset, len(d1_subset) - 1,
                              lookback=120, min_touches=2, tolerance_pct=1.5,
                              min_level_age=5, swing_order=3)


def collect_trades_for_vision(entry_data, d1_data, params):
    """Collect trades with entry_idx and level info for chart generation."""
    all_records = []

    for symbol in SYMBOLS:
        if symbol not in entry_data or symbol not in d1_data:
            continue
        df = entry_data[symbol]
        d1_df = d1_data[symbol]

        sma = df["close"].rolling(params["trend_sma"]).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        active_trade = None
        recent_breakouts: list[Breakout] = []
        cached_d1_levels = []
        last_check = 0
        last_d1_ts = None
        warmup = max(params["level_lookback"], params["trend_sma"]) + 10

        for i in range(warmup, len(df)):
            ts = df["ts"].iloc[i]

            if active_trade is not None:
                h, l = df["high"].iloc[i], df["low"].iloc[i]
                at = active_trade
                outcome, exit_price = None, None
                if at["is_long"]:
                    if l <= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif h >= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > params["max_hold"]: outcome, exit_price = "timeout", df["close"].iloc[i]
                else:
                    if h >= at["sl"]: outcome, exit_price = "loss", at["sl"]
                    elif l <= at["tp"]: outcome, exit_price = "win", at["tp"]
                    elif i - at["entry_idx"] > params["max_hold"]: outcome, exit_price = "timeout", df["close"].iloc[i]

                if outcome:
                    ep = at["entry_price"]
                    pnl_pct = ((exit_price - ep) / ep if at["is_long"] else (ep - exit_price) / ep) - 2 * FEE_BPS / 10000
                    record = {
                        "symbol": at["symbol"], "entry_idx": at["entry_idx"],
                        "entry_ts": at["entry_ts"],
                        "signal_type": at["signal_type"], "is_long": at["is_long"],
                        "level_price": at["level_price"],
                        "outcome": outcome, "pnl_pct": pnl_pct,
                    }
                    all_records.append(record)
                    active_trade = None
                else:
                    continue

            if last_d1_ts is None or (ts - last_d1_ts).total_seconds() > 86400:
                cached_d1_levels = get_d1_levels_at_time(d1_df, ts)
                last_d1_ts = ts

            if i - last_check >= params["check_interval"]:
                last_check = i
                new_brk = detect_breakouts(df, i, cached_d1_levels)
                recent_breakouts.extend(new_brk)
                recent_breakouts = [b for b in recent_breakouts if i - b.idx <= params["retest_window"]]

            close_i = df["close"].iloc[i]
            retest = detect_retests(df, i, recent_breakouts, params["retest_window"], trend)
            zakol = detect_zakol(df, i, cached_d1_levels, trend)
            best = retest if retest and (not zakol or retest[2] >= zakol[2]) else zakol

            if best:
                sig_type, lv, _ = best
                signal = build_signal(symbol, sig_type, lv, close_i, params["rr_ratio"])
                if signal is None:
                    continue

                active_trade = {
                    "entry_idx": i, "entry_price": close_i,
                    "sl": signal.sl, "tp": signal.tp,
                    "is_long": signal.is_long,
                    "signal_type": sig_type.value,
                    "symbol": symbol,
                    "entry_ts": ts,
                    "level_price": lv.price,
                }

    return pd.DataFrame(all_records)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    N = args.n
    print(f"=== Vision OOS Test ({N} trades) ===\n")

    if not API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set in .env")
        return

    d1_data = load_data("1d")
    data_4h = load_data("4h")
    print(f"D1: {len(d1_data)}, 4H: {len(data_4h)} symbols")

    params = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150,
    )

    print("Collecting trades...")
    trades = collect_trades_for_vision(data_4h, d1_data, params)
    trades = trades[trades["outcome"].isin(["win", "loss"])].copy()
    trades["target"] = (trades["outcome"] == "win").astype(int)
    trades["entry_ts"] = pd.to_datetime(trades["entry_ts"])
    trades = trades.sort_values("entry_ts")

    print(f"Total: {len(trades)} trades ({trades['target'].sum()}W/{(~trades['target'].astype(bool)).sum()}L)")

    # Take OOS trades (last 4 months)
    cutoff = trades["entry_ts"].max() - pd.Timedelta(days=120)
    oos = trades[trades["entry_ts"] >= cutoff]
    print(f"OOS (last 4mo): {len(oos)} trades")

    # Sample N trades (balanced: 50% W, 50% L)
    wins = oos[oos["target"] == 1]
    losses = oos[oos["target"] == 0]
    n_each = min(N // 2, len(wins), len(losses))
    sample = pd.concat([
        wins.sample(n_each, random_state=42),
        losses.sample(n_each, random_state=42),
    ]).sample(frac=1, random_state=42)  # shuffle

    print(f"Sampled: {len(sample)} trades ({n_each}W + {n_each}L)")
    print(f"Estimated cost: ~${len(sample) * 0.01:.2f}\n")

    # Score each trade with Vision
    results = []
    for i, (idx, row) in enumerate(sample.iterrows()):
        symbol = row["symbol"]
        df = data_4h[symbol]
        entry_idx = row["entry_idx"]
        is_long = row["is_long"]
        signal_type = row["signal_type"]
        level_price = row["level_price"]

        # Generate chart
        chart_b64 = generate_chart_b64(df, entry_idx, [level_price], signal_type, is_long)
        if not chart_b64:
            continue

        # Call Vision
        vision_result = call_vision(chart_b64, symbol, signal_type, is_long)
        score = vision_result.get("score", -1)

        if score < 0:
            print(f"  [{i+1}/{len(sample)}] {symbol} - FAILED: {vision_result.get('reason', '')[:60]}")
            continue

        results.append({
            "symbol": symbol,
            "signal_type": signal_type,
            "is_long": is_long,
            "outcome": row["outcome"],
            "target": row["target"],
            "pnl_pct": row["pnl_pct"],
            "vision_score": score,
            "reason": vision_result.get("reason", ""),
        })

        outcome_str = "WIN " if row["target"] == 1 else "LOSS"
        print(f"  [{i+1}/{len(sample)}] {symbol:14s} {signal_type:14s} -> score={score}, {outcome_str}")

        time.sleep(0.5)  # rate limit

    rdf = pd.DataFrame(results)

    # Save results
    REPORTS.mkdir(parents=True, exist_ok=True)
    rdf.to_csv(REPORTS / "vision_oos_results.csv", index=False)

    # Analysis
    print(f"\n{'='*65}")
    print(f"  RESULTS: {len(rdf)} scored trades")
    print(f"{'='*65}")

    if rdf.empty:
        print("  No results!")
        return

    # Correlation
    corr = rdf["vision_score"].corr(rdf["target"])
    print(f"\n  Correlation (score vs win): {corr:+.3f}")

    # Score distribution
    print(f"\n  Score distribution:")
    print(f"  {'Score':>6} {'N':>5} {'Wins':>5} {'WR':>6}")
    for s in range(1, 11):
        sub = rdf[rdf["vision_score"] == s]
        if len(sub) > 0:
            wr = sub["target"].mean()
            print(f"  {s:>6} {len(sub):>5} {sub['target'].sum():>5} {wr*100:>5.0f}%")

    # Score thresholds
    print(f"\n  Score threshold analysis:")
    print(f"  {'Threshold':>10} {'N':>5} {'WR':>6} {'PF':>6} {'AvgPnL':>8}")
    for thr in [5, 6, 7, 8]:
        sub = rdf[rdf["vision_score"] >= thr]
        if len(sub) >= 5:
            wr = sub["target"].mean()
            wins_pnl = sub[sub["target"]==1]["pnl_pct"]
            loss_pnl = sub[sub["target"]==0]["pnl_pct"]
            gw = wins_pnl.sum() if len(wins_pnl) > 0 else 0
            gl = abs(loss_pnl.sum()) if len(loss_pnl) > 0 else 1
            pf = gw / gl if gl > 0 else 0
            avg_pnl = sub["pnl_pct"].mean()
            print(f"  >={thr:>8} {len(sub):>5} {wr*100:>5.0f}% {pf:>5.2f} {avg_pnl*100:>+7.3f}%")

    # Wins vs Losses average score
    avg_win_score = rdf[rdf["target"]==1]["vision_score"].mean()
    avg_loss_score = rdf[rdf["target"]==0]["vision_score"].mean()
    print(f"\n  Avg score (wins):   {avg_win_score:.1f}")
    print(f"  Avg score (losses): {avg_loss_score:.1f}")
    print(f"  Delta:              {avg_win_score - avg_loss_score:+.1f}")


if __name__ == "__main__":
    main()
