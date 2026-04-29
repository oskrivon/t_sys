"""Unit tests for calendar event detection logic."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.calendar_signal.config import (
    CalendarConfig,
    EventDef,
    EventType,
    generate_fomc_events,
    generate_q_expiry_events,
)
from src.calendar_signal.events import (
    get_entry_event,
    get_events_for_date,
    get_exit_time,
    next_events,
    should_exit,
)


def _make_config(events: list[EventDef]) -> CalendarConfig:
    return CalendarConfig(events=events)


def _fomc_event(date: str = "2026-05-06") -> EventDef:
    return EventDef(
        event_type=EventType.PRE_FOMC_LONG,
        date=date, direction="long",
        entry_hour_utc=10, hold_hours=8,
    )


def _expiry_event(date: str = "2026-06-26") -> EventDef:
    return EventDef(
        event_type=EventType.POST_Q_EXPIRY_SHORT,
        date=date, direction="short",
        entry_hour_utc=8, hold_hours=24,
    )


class TestGetEventsForDate:
    def test_fomc_day(self):
        cfg = _make_config([_fomc_event("2026-05-06")])
        events = get_events_for_date(cfg, "2026-05-06")
        assert len(events) == 1
        assert events[0].event_type == EventType.PRE_FOMC_LONG

    def test_no_event_day(self):
        cfg = _make_config([_fomc_event("2026-05-06")])
        assert get_events_for_date(cfg, "2026-05-07") == []

    def test_multiple_events_same_day(self):
        cfg = _make_config([_fomc_event("2026-06-17"), _expiry_event("2026-06-17")])
        events = get_events_for_date(cfg, "2026-06-17")
        assert len(events) == 2


class TestGetEntryEvent:
    def test_within_window(self):
        cfg = _make_config([_fomc_event("2026-05-06")])
        now = datetime(2026, 5, 6, 10, 5, tzinfo=timezone.utc)
        event = get_entry_event(cfg, now, tolerance_minutes=30)
        assert event is not None
        assert event.direction == "long"

    def test_before_window(self):
        cfg = _make_config([_fomc_event("2026-05-06")])
        now = datetime(2026, 5, 6, 9, 50, tzinfo=timezone.utc)
        assert get_entry_event(cfg, now) is None

    def test_after_window(self):
        cfg = _make_config([_fomc_event("2026-05-06")])
        now = datetime(2026, 5, 6, 10, 45, tzinfo=timezone.utc)
        assert get_entry_event(cfg, now, tolerance_minutes=30) is None

    def test_wrong_day(self):
        cfg = _make_config([_fomc_event("2026-05-06")])
        now = datetime(2026, 5, 7, 10, 5, tzinfo=timezone.utc)
        assert get_entry_event(cfg, now) is None

    def test_expiry_entry_at_8(self):
        cfg = _make_config([_expiry_event("2026-06-26")])
        now = datetime(2026, 6, 26, 8, 3, tzinfo=timezone.utc)
        event = get_entry_event(cfg, now)
        assert event is not None
        assert event.direction == "short"
        assert event.hold_hours == 24


class TestShouldExit:
    def test_hold_not_expired(self):
        event = _fomc_event()
        entry = "2026-05-06T10:00:00+00:00"
        now = datetime(2026, 5, 6, 15, 0, tzinfo=timezone.utc)  # 5h into 8h hold
        assert should_exit(event, entry, now) is False

    def test_hold_expired(self):
        event = _fomc_event()
        entry = "2026-05-06T10:00:00+00:00"
        now = datetime(2026, 5, 6, 18, 1, tzinfo=timezone.utc)  # 8h+1min
        assert should_exit(event, entry, now) is True

    def test_hold_exact(self):
        event = _fomc_event()
        entry = "2026-05-06T10:00:00+00:00"
        now = datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)
        assert should_exit(event, entry, now) is True

    def test_24h_hold_expiry(self):
        event = _expiry_event()
        entry = "2026-06-26T08:00:00+00:00"
        now = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)
        assert should_exit(event, entry, now) is True

    def test_24h_hold_not_expired(self):
        event = _expiry_event()
        entry = "2026-06-26T08:00:00+00:00"
        now = datetime(2026, 6, 27, 7, 59, tzinfo=timezone.utc)
        assert should_exit(event, entry, now) is False


class TestGetExitTime:
    def test_fomc_exit(self):
        event = _fomc_event()
        exit_t = get_exit_time(event, "2026-05-06T10:00:00+00:00")
        assert exit_t == datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)

    def test_expiry_exit(self):
        event = _expiry_event()
        exit_t = get_exit_time(event, "2026-06-26T08:00:00+00:00")
        assert exit_t == datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)


class TestNextEvents:
    def test_returns_future_sorted(self):
        events = [_fomc_event("2026-06-17"), _expiry_event("2026-06-26"),
                  _fomc_event("2026-05-06")]
        cfg = CalendarConfig(events=sorted(events, key=lambda e: e.date))
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        upcoming = next_events(cfg, now, n=3)
        assert len(upcoming) == 3
        assert upcoming[0].date == "2026-05-06"

    def test_filters_past(self):
        events = [_fomc_event("2025-01-01"), _fomc_event("2027-01-27")]
        cfg = _make_config(events)
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        upcoming = next_events(cfg, now)
        assert len(upcoming) == 1
        assert upcoming[0].date == "2027-01-27"

    def test_limit_n(self):
        events = generate_fomc_events() + generate_q_expiry_events()
        events.sort(key=lambda e: e.date)
        cfg = _make_config(events)
        now = datetime(2025, 5, 1, tzinfo=timezone.utc)
        upcoming = next_events(cfg, now, n=3)
        assert len(upcoming) == 3


class TestGenerateEvents:
    def test_fomc_generates_pairs(self):
        events = generate_fomc_events(["2026-05-06"])
        assert len(events) == 2
        assert events[0].event_type == EventType.PRE_FOMC_LONG
        assert events[0].direction == "long"
        assert events[1].event_type == EventType.POST_FOMC_SHORT
        assert events[1].direction == "short"

    def test_q_expiry_generates_short(self):
        events = generate_q_expiry_events(["2026-06-26"])
        assert len(events) == 1
        assert events[0].direction == "short"
        assert events[0].hold_hours == 24
        assert events[0].entry_hour_utc == 8

    def test_fomc_entry_hour(self):
        events = generate_fomc_events(["2026-05-06"])
        pre = events[0]
        assert pre.entry_hour_utc == 10
        assert pre.hold_hours == 8
