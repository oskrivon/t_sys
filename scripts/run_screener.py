"""Run the Miro strategy screener.

Usage:
    python scripts/run_screener.py              # Run forever (aligned to 4h candle close)
    python scripts/run_screener.py --once       # Single scan
    python scripts/run_screener.py --no-telegram # Skip Telegram, print to console
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

import structlog
structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
)
log = structlog.get_logger()


async def main(args):
    from src.core.exchange.ccxt_adapter import CCXTAdapter
    from src.core.models.base import Exchange
    from src.screener.config import ScreenerConfig
    from src.screener.scanner import MiroScreener
    from src.ai.ml_scorer import MLScorer

    config = ScreenerConfig()

    # Exchange — Bybit (same as trading engine)
    exchange = CCXTAdapter(
        exchange=Exchange.BYBIT,
        testnet=False,
    )
    await exchange.connect()

    # ML scorer
    ml_scorer = MLScorer(model_path=config.model_path)
    if ml_scorer.load():
        log.info("ml_model_ready")
    else:
        log.warning("ml_model_not_available", path=config.model_path)

    # Vision scorer (optional)
    vision_scorer = None
    if config.vision_enabled:
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if api_key:
            from src.ai.vision_scorer import VisionScorer
            vision_scorer = VisionScorer(api_key=api_key)
            log.info("vision_scorer_ready")

    # Telegram notifier (optional)
    notifier = None
    if not args.no_telegram:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            from src.api.telegram.bot import TelegramNotifier
            notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
            log.info("telegram_notifier_ready")
        else:
            log.warning("telegram_not_configured")

    # Redis bus (optional — for publishing signals to paper trading service)
    redis_bus = None
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        try:
            from src.core.redis_bus import RedisBus
            redis_bus = RedisBus(redis_url)
            await redis_bus.connect()
            log.info("redis_bus_ready", url=redis_url)
        except Exception as e:
            log.warning("redis_bus_not_available", error=str(e))

    # Screener
    screener = MiroScreener(
        config=config,
        exchange=exchange,
        ml_scorer=ml_scorer,
        vision_scorer=vision_scorer,
        notifier=notifier,
        redis_bus=redis_bus,
    )

    try:
        if args.once:
            result = await screener.run_once()
            print(f"\nScan complete: {result.symbols_scanned} symbols, "
                  f"{result.signals_found} signals, {result.signals_alerted} alerted")
        else:
            await screener.run_forever()
    finally:
        await exchange.disconnect()


def cli():
    parser = argparse.ArgumentParser(description="Miro Strategy Screener")
    parser.add_argument("--once", action="store_true", help="Single scan, then exit")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram, print to console")
    args = parser.parse_args()

    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
