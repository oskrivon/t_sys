"""Calendar signal configuration and event dates."""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class EventType(str, Enum):
    PRE_FOMC_LONG = "pre_fomc_long"
    POST_FOMC_SHORT = "post_fomc_short"
    POST_Q_EXPIRY_SHORT = "post_q_expiry_short"


class EventDef(BaseModel):
    """A single calendar event that triggers a trade."""
    event_type: EventType
    date: str                  # "YYYY-MM-DD"
    direction: str             # "long" or "short"
    entry_hour_utc: int        # hour to enter
    hold_hours: int            # how long to hold
    description: str = ""


class CalendarConfig(BaseModel):
    """Full configuration for calendar signal strategy."""
    events: list[EventDef]
    stop_loss_pct: float = 0.0    # 0 = no SL (research: SL hurts for these)
    target_symbol: str = "BTC/USDT:USDT"
    db_path: str = "data/paper_trades.db"


# ── Known FOMC dates (decision day, 18:00 UTC) ────────────────────────

FOMC_DATES = [
    # 2025
    "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027
    "2027-01-27", "2027-03-17", "2027-05-05", "2027-06-16",
    "2027-07-28", "2027-09-22", "2027-11-03", "2027-12-15",
]


def _quarterly_expiry_fridays(start_year: int = 2025, end_year: int = 2027) -> list[str]:
    """Last Friday of Mar/Jun/Sep/Dec for each year."""
    dates = []
    for year in range(start_year, end_year + 1):
        for month in [3, 6, 9, 12]:
            if month == 12:
                last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = datetime(year, month + 1, 1) - timedelta(days=1)
            while last_day.weekday() != 4:
                last_day -= timedelta(days=1)
            dates.append(last_day.strftime("%Y-%m-%d"))
    return dates


Q_EXPIRY_DATES = _quarterly_expiry_fridays()


def generate_fomc_events(dates: list[str] | None = None) -> list[EventDef]:
    """Generate pre-FOMC LONG + post-FOMC SHORT events."""
    if dates is None:
        dates = FOMC_DATES
    events = []
    for d in dates:
        events.append(EventDef(
            event_type=EventType.PRE_FOMC_LONG,
            date=d, direction="long",
            entry_hour_utc=10, hold_hours=8,
            description="Pre-FOMC drift: LONG BTC 10:00-18:00 UTC",
        ))
        events.append(EventDef(
            event_type=EventType.POST_FOMC_SHORT,
            date=d, direction="short",
            entry_hour_utc=18, hold_hours=24,
            description="Post-FOMC dump: SHORT BTC 18:00 UTC +24h",
        ))
    return events


def generate_q_expiry_events(dates: list[str] | None = None) -> list[EventDef]:
    """Generate post-quarterly-expiry SHORT events."""
    if dates is None:
        dates = Q_EXPIRY_DATES
    return [
        EventDef(
            event_type=EventType.POST_Q_EXPIRY_SHORT,
            date=d, direction="short",
            entry_hour_utc=8, hold_hours=24,
            description="Post-Q-expiry dump: SHORT BTC from 08:00 UTC +24h",
        )
        for d in dates
    ]


def build_default_config() -> CalendarConfig:
    """Build config with all known future events."""
    events = generate_fomc_events() + generate_q_expiry_events()
    events.sort(key=lambda e: (e.date, e.entry_hour_utc))
    return CalendarConfig(events=events)


DEFAULT_CONFIG = build_default_config()
