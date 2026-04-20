#!/usr/bin/env python3
"""Trading engine daemon entry point.

Usage:
    python scripts/run_engine.py
    python scripts/run_engine.py --dry-run  # log signals but don't trade

Environment variables (.env):
    BYBIT_API_KEY, BYBIT_API_SECRET
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (optional)
    ENGINE_CAPITAL (default 50)
    ENGINE_FUNDING_THRESHOLD_BPS (default 10)
    ENGINE_FUNDING_LEVERAGE (default 10)
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

import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

from src.engine.daemon import TradingEngine


def main():
    parser = argparse.ArgumentParser(description="Trading Engine")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no trades")
    parser.add_argument("--capital", type=float,
                        default=float(os.getenv("ENGINE_CAPITAL", "50")))
    parser.add_argument("--funding-threshold", type=float,
                        default=float(os.getenv("ENGINE_FUNDING_THRESHOLD_BPS", "10")))
    parser.add_argument("--funding-leverage", type=int,
                        default=int(os.getenv("ENGINE_FUNDING_LEVERAGE", "10")))
    args = parser.parse_args()

    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    if not api_key or not api_secret:
        print("ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set in .env")
        sys.exit(1)

    engine = TradingEngine(
        bybit_api_key=api_key,
        bybit_api_secret=api_secret,
        total_capital=args.capital,
        redis_url=os.getenv("REDIS_URL", ""),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        strategies_config=os.getenv("STRATEGIES_CONFIG", "config/strategies.yml"),
    )

    asyncio.run(engine.start())


if __name__ == "__main__":
    main()
