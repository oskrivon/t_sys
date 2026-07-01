"""Data models for earnings backtest."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class EarningsEvent(BaseModel):
    """Single earnings release event."""
    symbol: str
    earnings_date: date
    report_time: str = "amc"  # "bmo" (before market open) or "amc" (after market close)
    eps_estimate: Optional[float] = None
    eps_actual: Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_actual: Optional[float] = None
    revenue_surprise_pct: Optional[float] = None


class TranscriptScore(BaseModel):
    """LLM-generated score from earnings transcript analysis."""
    sentiment: int  # -5 to +5
    guidance_direction: int  # -2 to +2
    management_confidence: int  # 1 to 5
    risk_flags: list[str] = []
    key_themes: list[str] = []
    composite_score: float  # -10 to +10
    reasoning: str = ""
