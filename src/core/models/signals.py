"""Shared signal schemas for cross-service communication.

All services serialize/deserialize signals through these models.
Designed for Redis pub/sub JSON transport.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    BREAKOUT = "breakout"
    RETEST = "retest"
    ZAKOL = "zakol"
    LONG_RETEST = "long_retest"
    SHORT_RETEST = "short_retest"
    LONG_ZAKOL = "long_zakol"
    SHORT_ZAKOL = "short_zakol"
    FUNDING_CAPTURE = "funding_capture"
    VOLUME_RANKING = "volume_ranking"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class ScreenerSignal(BaseModel):
    """Signal from the screener — a detected pattern."""
    signal_type: SignalType
    symbol: str
    direction: SignalDirection
    timeframe: str = "4h"
    entry_price: float
    sl: float
    tp: float
    rr_ratio: float = 3.0
    # Level info
    level_price: float = 0.0
    level_touches: int = 0
    level_score: float = 0.0
    # Scoring
    ml_score: float = 0.0
    vision_score: Optional[float] = None
    volume_ratio: float = 0.0
    # Metadata
    source: str = "screener"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_redis(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_redis(cls, data: dict) -> ScreenerSignal:
        payload = data.get("payload", data)
        return cls.model_validate(payload)


class FundingSignal(BaseModel):
    """Signal from funding capture strategy."""
    symbol: str
    direction: SignalDirection
    funding_rate: float
    funding_rate_bps: float
    next_funding_time: int  # ms timestamp
    leverage: int = 10
    exit_after_seconds: int = 15
    last_price: float = 0.0
    source: str = "funding_capture"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_redis(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_redis(cls, data: dict) -> FundingSignal:
        payload = data.get("payload", data)
        return cls.model_validate(payload)


class TradeEvent(BaseModel):
    """Trade open/close event from the engine."""
    event: str  # "open" or "close"
    symbol: str
    direction: SignalDirection
    strategy_id: str
    entry_price: float = 0.0
    exit_price: float = 0.0
    qty: float = 0.0
    leverage: int = 1
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    latency_ms: float = 0.0
    metadata: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_redis(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_redis(cls, data: dict) -> TradeEvent:
        payload = data.get("payload", data)
        return cls.model_validate(payload)


class EngineCommand(BaseModel):
    """Command sent to the engine (from Telegram bot)."""
    command: str  # "start", "stop", "status", "positions"
    strategy_id: Optional[str] = None
    params: dict = Field(default_factory=dict)
    source: str = "telegram"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_redis(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_redis(cls, data: dict) -> EngineCommand:
        payload = data.get("payload", data)
        return cls.model_validate(payload)
