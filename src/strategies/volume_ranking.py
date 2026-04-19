"""Volume Ranking Long/Short — systematic daily rebalance.

Cross-sectional: rank coins by volume acceleration (7d/30d),
long top half, short bottom half. Market-neutral.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import structlog

from src.strategies.base import (
    Strategy, StrategyConfig, StrategyType,
    TargetPosition, Side,
)

log = structlog.get_logger()


class VolumeRankingStrategy(Strategy):
    """Volume ranking long/short market-neutral strategy."""

    def __init__(self, config: StrategyConfig):
        super().__init__(config)
        self.short_window: int = config.params.get("short_window", 7)
        self.long_window: int = config.params.get("long_window", 30)
        self.top_pct: float = config.params.get("top_pct", 0.5)
        self.symbols: list[str] = config.params.get("symbols", [])
        self._volume_cache: dict[str, pd.DataFrame] = {}

    async def initialize(self, exchange) -> None:
        if not self.symbols:
            # Default: top 50 USDT pairs
            markets = exchange.load_markets()
            usdt = [s for s, m in markets.items()
                    if s.endswith("/USDT") and m.get("spot") and m.get("active")]
            self.symbols = usdt[:50]
        log.info("volume_ranking_init", symbols=len(self.symbols))

    async def on_tick(self, exchange) -> list[TargetPosition]:
        """Compute daily volume ratios and return target portfolio."""
        ratios = {}

        for symbol in self.symbols:
            try:
                candles = await exchange.fetch_ohlcv(
                    symbol, "1d", limit=self.long_window + 5
                )
                if not candles or len(candles) < self.long_window:
                    continue

                volumes = [c[5] for c in candles]
                vol_short = sum(volumes[-self.short_window:]) / self.short_window
                vol_long = sum(volumes[-self.long_window:]) / self.long_window

                if vol_long > 0:
                    ratios[symbol] = vol_short / vol_long
            except Exception as e:
                log.debug("volume_fetch_error", symbol=symbol, error=str(e))
                continue

        if len(ratios) < 10:
            log.warning("volume_ranking_insufficient", symbols=len(ratios))
            return []

        # Rank and split
        sorted_symbols = sorted(ratios.keys(), key=lambda s: ratios[s], reverse=True)
        n = len(sorted_symbols)
        n_long = int(n * self.top_pct)

        positions = []
        weight = 1.0 / n  # equal weight

        for i, symbol in enumerate(sorted_symbols):
            if i < n_long:
                side = Side.LONG
            else:
                side = Side.SHORT

            positions.append(TargetPosition(
                symbol=symbol,
                side=side,
                weight=weight,
                score=ratios[symbol],
            ))

        log.info("volume_ranking_computed",
                 n_symbols=n, n_long=n_long, n_short=n - n_long,
                 top_ratio=ratios[sorted_symbols[0]],
                 bottom_ratio=ratios[sorted_symbols[-1]])

        return positions
