"""Chart generation for trade signals.

Generates candlestick chart with S/R levels as PNG bytes.
Extracted from scripts/research/claude_vision_test.py.
"""
from __future__ import annotations

import io
from typing import Optional

import pandas as pd


def generate_chart(
    df: pd.DataFrame,
    entry_idx: int,
    levels: list[float],
    signal_type: str,
    is_long: bool,
    candles_before: int = 60,
    candles_after: int = 20,
) -> bytes:
    """Generate candlestick chart with S/R levels as PNG bytes.

    Args:
        df: OHLCV DataFrame with columns [ts, open, high, low, close, volume].
        entry_idx: Index of the entry candle.
        levels: List of level prices to draw.
        signal_type: Signal type string for label.
        is_long: True for long, False for short.
        candles_before: Candles to show before entry.
        candles_after: Candles to show after entry.

    Returns:
        PNG image as bytes. Empty bytes if chart generation fails.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    start = max(0, entry_idx - candles_before)
    end = min(len(df), entry_idx + candles_after)
    chunk = df.iloc[start:end].copy()

    if len(chunk) < 10:
        return b""

    fig, ax = plt.subplots(figsize=(12, 6), facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    # Candlesticks
    for i, (_, row) in enumerate(chunk.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = "#26a69a" if c >= o else "#ef5350"
        body_bottom = min(o, c)
        body_height = abs(c - o)
        ax.bar(i, body_height, bottom=body_bottom, width=0.6, color=color, edgecolor=color)
        ax.plot([i, i], [l, body_bottom], color=color, linewidth=0.8)
        ax.plot([i, i], [body_bottom + body_height, h], color=color, linewidth=0.8)

    # S/R levels
    price_range = chunk["high"].max() - chunk["low"].min()
    entry_price = df["close"].iloc[entry_idx]
    for lvl in levels:
        if chunk["low"].min() - price_range * 0.1 < lvl < chunk["high"].max() + price_range * 0.1:
            color = "#4caf50" if lvl < entry_price else "#f44336"
            ax.axhline(y=lvl, color=color, linewidth=1, alpha=0.7, linestyle="--")

    # Entry marker
    entry_local = entry_idx - start
    if 0 <= entry_local < len(chunk):
        ax.axvline(x=entry_local, color="#2196f3", linewidth=1.5, alpha=0.6, linestyle=":")
        arrow_color = "#26a69a" if is_long else "#ef5350"
        arrow_dir = 1 if is_long else -1
        label = "LONG" if is_long else "SHORT"
        ax.annotate(
            label,
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
    ax.set_ylabel("Price", color="#888")

    n_labels = min(8, len(chunk))
    step = max(1, len(chunk) // n_labels)
    tick_positions = list(range(0, len(chunk), step))
    tick_labels = []
    for i in tick_positions:
        ts = chunk["ts"].iloc[i]
        tick_labels.append(ts.strftime("%m/%d") if hasattr(ts, "strftime") else str(i))
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, color="#888", fontsize=8)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
