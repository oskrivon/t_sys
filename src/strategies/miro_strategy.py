"""Miro S/R Level Strategy — event-driven.

Wraps the existing MiroScreener to produce TradeSignals.
Pipeline: level detection → breakout/retest/zakol → ML filter → Vision filter → signal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog

from src.strategies.base import (
    Strategy, StrategyConfig, StrategyType,
    TradeSignal, Side,
)

log = structlog.get_logger()


class MiroStrategy(Strategy):
    """Miro breakout/retest/zakol with ML + optional Vision filter."""

    def __init__(self, config: StrategyConfig):
        super().__init__(config)
        self.screener = None
        self.vision_min_score: int = config.params.get("vision_min_score", 0)

    async def initialize(self, exchange) -> None:
        from src.screener.config import ScreenerConfig
        from src.screener.scanner import MiroScreener
        from src.ai.ml_scorer import MLScorer

        screener_config = ScreenerConfig()
        ml_scorer = MLScorer()

        # Vision scorer (optional)
        vision_scorer = None
        if self.vision_min_score > 0:
            try:
                from src.ai.vision_scorer import VisionScorer
                import os
                api_key = os.getenv("OPENROUTER_API_KEY", "")
                if api_key:
                    vision_scorer = VisionScorer(api_key=api_key)
                    screener_config.vision_enabled = True
                    log.info("vision_enabled", min_score=self.vision_min_score)
            except Exception as e:
                log.warning("vision_init_failed", error=str(e))

        self.screener = MiroScreener(
            config=screener_config,
            exchange=exchange,
            ml_scorer=ml_scorer,
            vision_scorer=vision_scorer,
        )

    async def on_tick(self, exchange) -> list[TradeSignal]:
        if not self.screener:
            return []

        result = await self.screener.run_once()
        signals = []

        for signal in result.signals:
            # Vision filter
            if self.vision_min_score > 0 and (
                signal.vision_score is None or signal.vision_score < self.vision_min_score
            ):
                continue

            signals.append(TradeSignal(
                strategy_id=self.strategy_id,
                symbol=signal.symbol,
                side=Side.LONG if signal.is_long else Side.SHORT,
                entry_price=signal.entry_price,
                sl=signal.sl,
                tp=signal.tp,
                confidence=signal.ml_score or 0.0,
                metadata={
                    "signal_type": signal.signal_type.value,
                    "level_price": signal.level.price,
                    "level_score": signal.level.score,
                    "ml_score": signal.ml_score,
                    "vision_score": signal.vision_score,
                    "volume_ratio": signal.volume_ratio,
                },
                timestamp=signal.timestamp or datetime.now(timezone.utc),
            ))

        return signals
