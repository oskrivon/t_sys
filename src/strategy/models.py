"""Strategy models — Level, Signal, ScreenerResult."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SignalType(str, Enum):
    LONG_RETEST = "long_retest"
    SHORT_RETEST = "short_retest"
    LONG_ZAKOL = "long_zakol"
    SHORT_ZAKOL = "short_zakol"


class Level(BaseModel):
    """Detected S/R level from clustered swing points."""

    price: float
    touches: int
    zone_high: float
    zone_low: float
    first_idx: int
    last_idx: int
    score: int

    @property
    def zone_width(self) -> float:
        return self.zone_high - self.zone_low


class Breakout(BaseModel):
    """Tracked breakout awaiting retest."""

    level: Level
    direction: str  # "long" or "short"
    idx: int


class Signal(BaseModel):
    """Detected trade signal."""

    symbol: str
    signal_type: SignalType
    level: Level
    entry_price: float
    sl: float
    tp: float
    is_long: bool
    timestamp: Optional[datetime] = None

    # Scoring
    level_score: int = 0
    ml_score: Optional[float] = None
    vision_score: Optional[int] = None
    volume_ratio: Optional[float] = None

    @property
    def sl_pct(self) -> float:
        return abs(self.entry_price - self.sl) / self.entry_price * 100

    @property
    def tp_pct(self) -> float:
        return abs(self.tp - self.entry_price) / self.entry_price * 100

    @property
    def rr_ratio(self) -> float:
        sl_dist = abs(self.entry_price - self.sl)
        tp_dist = abs(self.tp - self.entry_price)
        return tp_dist / sl_dist if sl_dist > 0 else 0


class ScreenerResult(BaseModel):
    """Result of a single screener scan."""

    timestamp: datetime
    symbols_scanned: int
    signals_found: int
    signals_alerted: int
    signals: list[Signal] = []
