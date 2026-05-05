"""Telegram Bot Service — bidirectional.

Receives commands from user → publishes to Redis.
Subscribes to Redis notifications → forwards to user.
Also keeps the original signal alert functionality.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import structlog
from telegram import Bot
from telegram.ext import Application, CommandHandler

from src.core.redis_bus import RedisBus, CH_NOTIFICATIONS_TG
from .commands import (
    cmd_status, cmd_positions, cmd_balance,
    cmd_start_strategy, cmd_stop_strategy,
    cmd_paper, cmd_help,
)

logger = structlog.get_logger()


class TelegramService:
    """Full Telegram bot: commands + notification forwarding."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        redis_url: str,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._redis_url = redis_url
        self._bus: Optional[RedisBus] = None
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None

    async def start(self) -> None:
        # Connect Redis
        self._bus = RedisBus(self._redis_url)
        await self._bus.connect()
        self._bus.start_heartbeat("telegram-bot")

        # Subscribe to notifications channel
        self._bus.on(CH_NOTIFICATIONS_TG, self._on_notification)

        # Build Telegram Application
        self._app = Application.builder().token(self._token).build()
        self._bot = self._app.bot

        # Inject redis_bus into bot_data so handlers can access it
        self._app.bot_data["redis_bus"] = self._bus
        self._app.bot_data["chat_id"] = self._chat_id

        # Register command handlers
        self._app.add_handler(CommandHandler("status", cmd_status))
        self._app.add_handler(CommandHandler("positions", cmd_positions))
        self._app.add_handler(CommandHandler("balance", cmd_balance))
        self._app.add_handler(CommandHandler("start", cmd_start_strategy))
        self._app.add_handler(CommandHandler("stop", cmd_stop_strategy))
        self._app.add_handler(CommandHandler("paper", cmd_paper))
        self._app.add_handler(CommandHandler("help", cmd_help))

        logger.info("telegram_service_started")

        # Run both: Telegram polling + Redis listener
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

        try:
            await self._bus.run()
        finally:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def _on_notification(self, message: dict) -> None:
        """Forward Redis notification to Telegram chat."""
        payload = message.get("payload", {})
        text = payload.get("text", "")
        if not text:
            text = json.dumps(payload, indent=2, default=str)[:4000]

        source = message.get("source", "")
        if source:
            text = f"[{source}] {text}"

        try:
            await self._bot.send_message(chat_id=self._chat_id, text=text)
        except Exception:
            logger.exception("telegram_forward_error")

    async def shutdown(self) -> None:
        if self._bus:
            await self._bus.stop()
            await self._bus.disconnect()
