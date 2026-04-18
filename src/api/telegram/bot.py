"""Telegram notifier — sends signal alerts with charts."""
from __future__ import annotations

import asyncio
import io
from typing import Optional

import structlog
import pandas as pd

from src.strategy.models import Signal
from src.ai.chart_generator import generate_chart

from .formatters import format_signal_message

log = structlog.get_logger()


class TelegramNotifier:
    """Sends trade signal alerts via Telegram bot."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot = None

    async def _get_bot(self):
        if self._bot is None:
            from telegram import Bot
            self._bot = Bot(token=self.bot_token)
        return self._bot

    async def send_signal_alert(
        self,
        signal: Signal,
        df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Send a signal alert with optional chart attachment.

        Args:
            signal: The trade signal to alert.
            df: OHLCV DataFrame for chart generation.
        """
        bot = await self._get_bot()
        message = format_signal_message(signal)

        # Generate chart if we have data
        chart_bytes = None
        if df is not None and len(df) > 20:
            entry_idx = len(df) - 1
            level_prices = [signal.level.price]

            chart_bytes = await asyncio.to_thread(
                generate_chart,
                df, entry_idx, level_prices,
                signal.signal_type.value, signal.is_long,
                candles_before=60, candles_after=0,
            )

        try:
            if chart_bytes:
                await bot.send_photo(
                    chat_id=self.chat_id,
                    photo=io.BytesIO(chart_bytes),
                    caption=message,
                )
            else:
                await bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                )

            log.info("alert_sent", symbol=signal.symbol, type=signal.signal_type.value)
        except Exception as e:
            log.error("telegram_send_error", error=str(e))
            raise

    async def send_text(self, text: str) -> None:
        """Send a plain text message."""
        bot = await self._get_bot()
        await bot.send_message(chat_id=self.chat_id, text=text)
