"""Screener state persistence — survives between runs."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog
from pydantic import BaseModel

from src.strategy.models import Breakout, Level

log = structlog.get_logger()


class BreakoutState(BaseModel):
    """Serializable breakout for state persistence."""

    level_price: float
    level_zone_high: float
    level_zone_low: float
    level_score: int
    level_touches: int
    direction: str
    idx: int
    candle_ts: Optional[str] = None

    def to_breakout(self) -> Breakout:
        return Breakout(
            level=Level(
                price=self.level_price,
                touches=self.level_touches,
                zone_high=self.level_zone_high,
                zone_low=self.level_zone_low,
                first_idx=0,
                last_idx=0,
                score=self.level_score,
            ),
            direction=self.direction,
            idx=self.idx,
        )

    @classmethod
    def from_breakout(cls, brk: Breakout, candle_ts: str = "") -> BreakoutState:
        return cls(
            level_price=brk.level.price,
            level_zone_high=brk.level.zone_high,
            level_zone_low=brk.level.zone_low,
            level_score=brk.level.score,
            level_touches=brk.level.touches,
            direction=brk.direction,
            idx=brk.idx,
            candle_ts=candle_ts,
        )


class ScreenerState(BaseModel):
    """Persisted screener state between runs."""

    last_run_ts: Optional[str] = None
    recent_breakouts: dict[str, list[BreakoutState]] = {}  # symbol -> breakouts
    alerted_signals: list[str] = []  # "symbol|signal_type|ts" dedup keys

    def add_alert_key(self, symbol: str, signal_type: str, ts: str) -> None:
        key = f"{symbol}|{signal_type}|{ts}"
        if key not in self.alerted_signals:
            self.alerted_signals.append(key)

    def was_alerted(self, symbol: str, signal_type: str, ts: str) -> bool:
        return f"{symbol}|{signal_type}|{ts}" in self.alerted_signals

    def cleanup(self, max_breakout_age: int = 15, max_alerts: int = 500) -> None:
        """Remove stale data."""
        # Keep only recent alerts
        if len(self.alerted_signals) > max_alerts:
            self.alerted_signals = self.alerted_signals[-max_alerts:]


def load_state(path: str) -> ScreenerState:
    """Load state from JSON file, or return empty state."""
    p = Path(path)
    if not p.exists():
        return ScreenerState()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return ScreenerState.model_validate(data)
    except Exception as e:
        log.warning("state_load_error", error=str(e), path=path)
        return ScreenerState()


def save_state(state: ScreenerState, path: str) -> None:
    """Save state to JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2), encoding="utf-8")
