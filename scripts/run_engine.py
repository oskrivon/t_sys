#!/usr/bin/env python3
"""Trading engine daemon entry point.

Usage:
    python scripts/run_engine.py                    # Bybit (default)
    python scripts/run_engine.py --exchange binance  # Binance
    python scripts/run_engine.py --dry-run           # log signals but don't trade

Environment variables (.env):
    ENGINE_EXCHANGE (default bybit)
    BYBIT_API_KEY, BYBIT_API_SECRET
    BINANCE_API_KEY, BINANCE_API_SECRET
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (optional)
    ENGINE_CAPITAL (default 50)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import logging
from logging.handlers import RotatingFileHandler
import structlog

log_dir = ROOT / "data" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

# Per-exchange log file: engine.log (bybit), engine_binance.log, etc.
_exchange_suffix = os.getenv("ENGINE_EXCHANGE", "bybit")
_log_name = "engine.log" if _exchange_suffix == "bybit" else f"engine_{_exchange_suffix}.log"
file_handler = RotatingFileHandler(
    log_dir / _log_name, maxBytes=10_000_000, backupCount=5, encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
    handlers=[console_handler, file_handler],
)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)

from src.engine.daemon import TradingEngine


def main():
    parser = argparse.ArgumentParser(description="Trading Engine")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no trades")
    parser.add_argument("--exchange", type=str,
                        default=os.getenv("ENGINE_EXCHANGE", "bybit"),
                        choices=["bybit", "binance"],
                        help="Exchange to trade on (default: bybit)")
    parser.add_argument("--capital", type=float,
                        default=float(os.getenv("ENGINE_CAPITAL", "50")))
    args = parser.parse_args()

    exchange = args.exchange.lower()

    # Resolve API keys: try exchange-specific first, then generic
    key_prefix = exchange.upper()
    api_key = os.getenv(f"{key_prefix}_API_KEY") or os.getenv("API_KEY")
    api_secret = os.getenv(f"{key_prefix}_API_SECRET") or os.getenv("API_SECRET")
    if not api_key or not api_secret:
        print(f"ERROR: {key_prefix}_API_KEY and {key_prefix}_API_SECRET must be set in .env")
        sys.exit(1)

    engine = TradingEngine(
        api_key=api_key,
        api_secret=api_secret,
        exchange=exchange,
        total_capital=args.capital,
        redis_url=os.getenv("REDIS_URL", ""),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        strategies_config=os.getenv("STRATEGIES_CONFIG", "config/strategies.yml"),
    )

    asyncio.run(engine.start())


if __name__ == "__main__":
    main()
