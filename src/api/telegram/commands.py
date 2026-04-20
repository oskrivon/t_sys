"""Telegram bot command handlers."""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from src.core.redis_bus import RedisBus, CH_COMMANDS_ENGINE
from src.core.models.signals import EngineCommand

logger = structlog.get_logger()


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show engine and paper trading status."""
    bus: RedisBus = context.bot_data.get("redis_bus")
    if not bus:
        await update.message.reply_text("Redis not connected")
        return

    # Request status from engine via Redis
    cmd = EngineCommand(command="status")
    await bus.publish(CH_COMMANDS_ENGINE, cmd.to_redis(), source="telegram")

    # Get cached status from Redis KV (engine writes it periodically)
    status = await bus.get("engine:status")
    paper_status = await bus.get("paper:status")

    lines = ["=== Trading Platform ===\n"]

    if status:
        lines.append(f"Engine: {status}")
    else:
        lines.append("Engine: no status (not running?)")

    if paper_status:
        lines.append(f"Paper: {paper_status}")

    await update.message.reply_text("\n".join(lines))


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all open positions."""
    bus: RedisBus = context.bot_data.get("redis_bus")
    if not bus:
        await update.message.reply_text("Redis not connected")
        return

    positions = await bus.get("engine:positions")
    if positions:
        await update.message.reply_text(f"Open positions:\n{positions}")
    else:
        await update.message.reply_text("No open positions")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show account balance."""
    bus: RedisBus = context.bot_data.get("redis_bus")
    if not bus:
        await update.message.reply_text("Redis not connected")
        return

    balance = await bus.get("engine:balance")
    if balance:
        await update.message.reply_text(f"Balance: {balance}")
    else:
        await update.message.reply_text("Balance: unknown (engine not running?)")


async def cmd_start_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start <strategy_id> — enable a strategy."""
    bus: RedisBus = context.bot_data.get("redis_bus")
    if not bus:
        await update.message.reply_text("Redis not connected")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /start <strategy_id>\nExamples: /start funding_capture")
        return

    strategy_id = args[0]
    cmd = EngineCommand(command="start", strategy_id=strategy_id)
    await bus.publish(CH_COMMANDS_ENGINE, cmd.to_redis(), source="telegram")
    await update.message.reply_text(f"Sent start command for: {strategy_id}")


async def cmd_stop_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop <strategy_id> — disable a strategy."""
    bus: RedisBus = context.bot_data.get("redis_bus")
    if not bus:
        await update.message.reply_text("Redis not connected")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /stop <strategy_id>")
        return

    strategy_id = args[0]
    cmd = EngineCommand(command="stop", strategy_id=strategy_id)
    await bus.publish(CH_COMMANDS_ENGINE, cmd.to_redis(), source="telegram")
    await update.message.reply_text(f"Sent stop command for: {strategy_id}")


async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show paper trading statistics."""
    bus: RedisBus = context.bot_data.get("redis_bus")
    if not bus:
        await update.message.reply_text("Redis not connected")
        return

    stats = await bus.get("paper:stats")
    if stats:
        await update.message.reply_text(f"Paper Trading:\n{stats}")
    else:
        await update.message.reply_text("Paper trading: no stats available")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands."""
    text = (
        "Trading Platform Commands\n"
        "========================\n\n"
        "Monitoring:\n"
        "  /status — engine status, active strategies, WS connection\n"
        "  /positions — list open live positions (symbol, side, entry)\n"
        "  /balance — account balance on Bybit\n"
        "  /paper — paper trading statistics (WR, PnL, trades)\n\n"
        "Engine Control:\n"
        "  /start <strategy> — enable a strategy\n"
        "  /stop <strategy> — disable a strategy\n\n"
        "Available strategies:\n"
        "  funding_capture — funding rate >10bps, 10x leverage\n"
        "  miro_breakout — S/R levels + ML + Vision (coming soon)\n"
        "  volume_ranking — volume momentum L/S (coming soon)\n\n"
        "Info:\n"
        "  /help — this message\n\n"
        "Alerts are sent automatically:\n"
        "  - Screener signals (breakout/retest patterns)\n"
        "  - Funding capture entry/exit\n"
        "  - Paper trade TP/SL hits\n"
        "  - Engine warnings (WS disconnect, errors)"
    )
    await update.message.reply_text(text)
