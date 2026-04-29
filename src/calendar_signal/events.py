"""Event detection logic — pure functions, zero I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.calendar_signal.config import CalendarConfig, EventDef


def get_events_for_date(config: CalendarConfig, date_str: str) -> list[EventDef]:
    """Get all events scheduled for a specific date."""
    return [e for e in config.events if e.date == date_str]


def get_entry_event(
    config: CalendarConfig,
    now: datetime,
    tolerance_minutes: int = 30,
) -> EventDef | None:
    """Find an event that should be entered right now.

    Returns the first event whose entry time is within tolerance_minutes
    of `now`, and that hasn't passed its entry window yet.
    """
    today = now.strftime("%Y-%m-%d")
    for event in config.events:
        if event.date != today:
            continue
        entry_time = datetime.strptime(event.date, "%Y-%m-%d").replace(
            hour=event.entry_hour_utc, tzinfo=timezone.utc,
        )
        # Entry window: [entry_time, entry_time + tolerance]
        if entry_time <= now <= entry_time + timedelta(minutes=tolerance_minutes):
            return event
    return None


def should_exit(event: EventDef, entry_time_str: str, now: datetime) -> bool:
    """Check if hold period has expired."""
    entry_time = datetime.fromisoformat(entry_time_str)
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    exit_time = entry_time + timedelta(hours=event.hold_hours)
    return now >= exit_time


def get_exit_time(event: EventDef, entry_time_str: str) -> datetime:
    """Compute expected exit time."""
    entry_time = datetime.fromisoformat(entry_time_str)
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    return entry_time + timedelta(hours=event.hold_hours)


def next_events(config: CalendarConfig, now: datetime, n: int = 5) -> list[EventDef]:
    """Get next N upcoming events."""
    today = now.strftime("%Y-%m-%d")
    future = [e for e in config.events if e.date >= today]
    return future[:n]


def event_to_dict(event: EventDef) -> dict:
    """Convert event to dict for JSON storage."""
    return {
        "event_type": event.event_type.value,
        "date": event.date,
        "direction": event.direction,
        "entry_hour_utc": event.entry_hour_utc,
        "hold_hours": event.hold_hours,
    }
