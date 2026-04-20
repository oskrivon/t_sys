#!/usr/bin/env python3
"""Paper Trading Service entry point."""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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

from src.paper_trading.service import PaperTradingService


async def main():
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    service = PaperTradingService(redis_url=redis_url)
    try:
        await service.start()
    except KeyboardInterrupt:
        await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
