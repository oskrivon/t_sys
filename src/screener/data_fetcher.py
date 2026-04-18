"""Async batched OHLCV data fetcher."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import structlog

from src.core.models.base import Timeframe

log = structlog.get_logger()

# Map string timeframes to Timeframe enum
_TF_MAP = {tf.value: tf for tf in Timeframe}


async def fetch_all_candles(
    exchange,
    symbols: list[str],
    timeframe: str = "4h",
    limit: int = 210,
    concurrency: int = 10,
    cache_dir: str = "data/processed/candles",
) -> dict[str, pd.DataFrame]:
    """Fetch OHLCV data for multiple symbols concurrently.

    Args:
        exchange: CCXTAdapter instance.
        symbols: List of trading pairs.
        timeframe: Candle timeframe.
        limit: Number of candles to fetch per symbol.
        concurrency: Max concurrent API requests.
        cache_dir: Directory to cache parquet files.

    Returns:
        Dict of symbol -> DataFrame with columns [ts, open, high, low, close, volume].
    """
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, pd.DataFrame] = {}
    tf_enum = _TF_MAP.get(timeframe, Timeframe.H4)

    async def fetch_one(symbol: str) -> None:
        async with sem:
            try:
                candles = await exchange.get_candles(symbol, tf_enum, limit=limit)
                if not candles:
                    log.warning("no_candles", symbol=symbol)
                    return

                rows = []
                for c in candles:
                    rows.append({
                        "ts": c.timestamp,
                        "open": float(c.open),
                        "high": float(c.high),
                        "low": float(c.low),
                        "close": float(c.close),
                        "volume": float(c.volume),
                    })

                df = pd.DataFrame(rows)
                df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
                results[symbol] = df

            except Exception as e:
                log.warning("candle_fetch_error", symbol=symbol, error=str(e))

    tasks = [fetch_one(s) for s in symbols]
    await asyncio.gather(*tasks)

    log.info("candles_fetched", total=len(results), requested=len(symbols))
    return results
