"""Telegram message formatting for trade signals."""
from __future__ import annotations

from src.strategy.models import Signal


def _fmt_price(price: float) -> str:
    """Format price with appropriate precision."""
    if price >= 100:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    if price >= 0.01:
        return f"${price:.5f}"
    return f"${price:.8f}"


def format_signal_message(signal: Signal) -> str:
    """Format a Signal into a Telegram message.

    Returns a plain-text message with key trade info.
    """
    direction = "Long" if signal.is_long else "Short"
    sig_name = signal.signal_type.value.replace("_", " ").title()

    lines = [
        f"[SIGNAL] {signal.symbol} 4H — {direction} {sig_name}",
        "",
        f"Level: {_fmt_price(signal.level.price)} ({signal.level.touches} touches, score {signal.level_score})",
        f"Entry: {_fmt_price(signal.entry_price)}",
        f"SL: {_fmt_price(signal.sl)} (-{signal.sl_pct:.1f}%)",
        f"TP: {_fmt_price(signal.tp)} (+{signal.tp_pct:.1f}%)",
        f"R:R: 1:{signal.rr_ratio:.1f}",
    ]

    # ML score
    if signal.ml_score is not None:
        lines.append(f"\nML: {signal.ml_score:.0%} P(win)")

    # Vision score
    if signal.vision_score is not None:
        lines.append(f"Vision: {signal.vision_score}/10")

    # Volume
    if signal.volume_ratio is not None:
        label = " (coin in play)" if signal.volume_ratio >= 1.5 else ""
        lines.append(f"Volume: {signal.volume_ratio:.1f}x avg{label}")

    return "\n".join(lines)
