"""Coins-in-play detector — finds active coins by volume spike."""
from __future__ import annotations

from typing import Optional

import structlog

log = structlog.get_logger()


class CoinsInPlayDetector:
    """Detects active coins via 24h volume analysis."""

    def __init__(
        self,
        base_symbols: list[str],
        volume_ratio_threshold: float = 1.5,
        max_coins: int = 60,
    ):
        self.base_symbols = base_symbols
        self.volume_ratio_threshold = volume_ratio_threshold
        self.max_coins = max_coins

    async def get_active_coins(self, exchange) -> list[str]:
        """Get list of coins to scan.

        Combines base watchlist with volume-spike detection.

        Args:
            exchange: CCXTAdapter instance with async get_tickers().

        Returns:
            List of symbols to scan.
        """
        try:
            tickers = await exchange.get_tickers()
        except Exception as e:
            log.warning("tickers_fetch_failed", error=str(e))
            return self.base_symbols[:self.max_coins]

        # Filter USDT pairs with valid data (exclude delisted/dead coins)
        usdt_tickers = []
        for t in tickers:
            if not t.symbol.endswith("/USDT"):
                continue
            # Must have volume, bid, and ask (no bid/ask = delisted)
            if t.volume_24h is None or float(t.volume_24h) <= 0:
                continue
            if t.bid is None or t.ask is None:
                continue
            usdt_tickers.append(t)

        if not usdt_tickers:
            return self.base_symbols[:self.max_coins]

        # Start with base watchlist
        active: set[str] = set(self.base_symbols)

        # Add high-volume coins not in base list
        # Sort by 24h quote volume (price * volume) descending
        sorted_by_volume = sorted(
            usdt_tickers,
            key=lambda t: float(t.last) * float(t.volume_24h) if t.last and t.volume_24h else 0,
            reverse=True,
        )

        # Add top volume coins
        for t in sorted_by_volume[:self.max_coins]:
            active.add(t.symbol)

        # Also add big movers (abs change > 5%)
        for t in usdt_tickers:
            if t.change_24h_pct is not None and abs(float(t.change_24h_pct)) > 5:
                active.add(t.symbol)

        result = list(active)[:self.max_coins]
        log.info(
            "coins_in_play",
            base=len(self.base_symbols),
            active=len(result),
            added=len(result) - len(self.base_symbols),
        )
        return result
