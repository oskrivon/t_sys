"""Telegram alerting for weekend signals."""
from __future__ import annotations

from typing import Any, Optional

import structlog

from src.weekend.config import WeekendConfig
from src.weekend.ensemble import EnsembleResult

logger = structlog.get_logger()


def format_signal_message(
    result: EnsembleResult,
    btc_price: Optional[float],
    sl_price: Optional[float],
    config: WeekendConfig,
) -> str:
    """Format Friday signal for Telegram / console."""
    lines = [
        "=" * 40,
        "WEEKEND 5-WAY ENSEMBLE SIGNAL",
        f"Date: {result.date}",
        "=" * 40,
        "",
        "Predictors:",
    ]

    for pr in result.predictors:
        arrow = "UP" if pr.vote > 0 else "DOWN"
        lines.append(f"  {pr.name:<20s} {pr.value_pct:+.2f}% -> {arrow}")

    if result.failed_predictors:
        for name in result.failed_predictors:
            lines.append(f"  {name:<20s} FAILED")

    lines.append("")
    lines.append(f"Vote: {result.vote_sum:+d}/{result.total_votes} "
                 f"(need {config.majority_threshold})")
    lines.append("")

    if result.direction:
        dir_label = "LONG (buy)" if result.direction == "long" else "SHORT (sell)"
        lines.append(f"SIGNAL: {dir_label} BTC")
        lines.append(f"Consensus: {result.consensus}/{result.total_votes} "
                     f"({result.confidence:.0%})")
        if btc_price is not None:
            lines.append(f"BTC: ${btc_price:,.0f}")
        if sl_price is not None:
            lines.append(f"SL ({config.stop_loss_pct:.0%}): ${sl_price:,.0f}")
        lines.append(f"Entry: Friday {config.entry_hour_utc}:00 UTC")
        lines.append(f"Exit: Sunday {config.exit_hour_utc}:00 UTC")
    else:
        lines.append("NO TRADE — insufficient consensus")

    lines.extend([
        "",
        "Strategy: 5-WAY(KWEB+EWJ+XLK+XLE+USDJPY) OOS Sharpe 1.83",
        "=" * 40,
    ])
    return "\n".join(lines)


def format_settlement_message(
    signal_date: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    pnl_pct: float,
    sl_hit: bool,
    stats: dict[str, Any],
) -> str:
    """Format Sunday settlement message."""
    lines = [
        "=" * 40,
        "WEEKEND TRADE SETTLED",
        f"Date: {signal_date}",
        "=" * 40,
        "",
        f"Direction: {direction.upper()}",
        f"Entry: ${entry_price:,.0f}",
        f"Exit: ${exit_price:,.0f}",
        f"P&L: {pnl_pct:+.2f}%{'  (SL HIT)' if sl_hit else ''}",
    ]

    if stats.get("total", 0) > 0:
        lines.extend([
            "",
            f"Cumulative ({stats['total']} trades):",
            f"  WR: {stats['win_rate']:.0f}%  Total P&L: {stats['total_pnl']:+.1f}%",
            f"  Avg: {stats['avg_pnl']:+.2f}%  Best: {stats['best']:+.2f}%  "
            f"Worst: {stats['worst']:+.2f}%",
        ])

    lines.append("=" * 40)
    return "\n".join(lines)


async def send_telegram(text: str, bot_token: str, chat_id: str) -> bool:
    """Send message to Telegram. Returns True on success."""
    try:
        from telegram import Bot
        bot = Bot(token=bot_token)
        await bot.send_message(chat_id=chat_id, text=text)
        logger.info("telegram_sent", length=len(text))
        return True
    except Exception as e:
        logger.error("telegram_failed", error=str(e))
        return False
