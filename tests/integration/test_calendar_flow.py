"""Integration tests for calendar signal flow."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.calendar_signal.config import (
    CalendarConfig,
    EventDef,
    EventType,
    generate_fomc_events,
    generate_q_expiry_events,
)
from src.calendar_signal.runner import run_entry, run_exit, run_sl_check
from src.calendar_signal.state import (
    get_history,
    get_open_trades,
    get_stats,
    get_trade,
    init_db,
    mark_entry,
    mark_exit,
    record_signal,
)


@pytest.fixture
def tmp_config(tmp_path):
    events = generate_fomc_events(["2026-05-06"]) + generate_q_expiry_events(["2026-06-26"])
    events.sort(key=lambda e: (e.date, e.entry_hour_utc))
    return CalendarConfig(
        events=events,
        db_path=str(tmp_path / "test_calendar.db"),
    )


@pytest.fixture
def db(tmp_config):
    conn = init_db(tmp_config.db_path)
    yield conn
    conn.close()


class TestStateDB:
    def test_record_and_retrieve(self, db):
        row_id = record_signal(db, "2026-05-06", "pre_fomc_long", "long", 8)
        assert row_id is not None
        trade = get_trade(db, "2026-05-06", "pre_fomc_long")
        assert trade["direction"] == "long"
        assert trade["status"] == "pending"

    def test_idempotent(self, db):
        id1 = record_signal(db, "2026-05-06", "pre_fomc_long", "long", 8)
        id2 = record_signal(db, "2026-05-06", "pre_fomc_long", "long", 8)
        assert id1 is not None
        assert id2 is None

    def test_same_date_different_type(self, db):
        id1 = record_signal(db, "2026-05-06", "pre_fomc_long", "long", 8)
        id2 = record_signal(db, "2026-05-06", "post_fomc_short", "short", 24)
        assert id1 is not None
        assert id2 is not None

    def test_lifecycle(self, db):
        record_signal(db, "2026-05-06", "pre_fomc_long", "long", 8)
        mark_entry(db, "2026-05-06", "pre_fomc_long", 95000.0)
        trades = get_open_trades(db)
        assert len(trades) == 1
        mark_exit(db, trades[0]["id"], 96000.0, 1.05)
        trade = get_trade(db, "2026-05-06", "pre_fomc_long")
        assert trade["status"] == "closed"
        assert trade["pnl_pct"] == pytest.approx(1.05)

    def test_stats(self, db):
        for i, (et, d, pnl) in enumerate([
            ("pre_fomc_long", "long", 0.8),
            ("post_fomc_short", "short", -0.3),
            ("post_q_expiry_short", "short", 1.2),
        ]):
            date = f"2026-05-{10+i:02d}"
            record_signal(db, date, et, d, 8)
            mark_entry(db, date, et, 95000.0)
            trades = get_open_trades(db)
            mark_exit(db, trades[-1]["id"], 95000.0, pnl)
        stats = get_stats(db)
        assert stats["total"] == 3
        assert stats["wins"] == 2
        assert stats["best"] == 1.2


class TestRunEntry:
    @pytest.mark.asyncio
    async def test_fomc_entry(self, tmp_config):
        now = datetime(2026, 5, 6, 10, 3, tzinfo=timezone.utc)
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=95000.0),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            event = await run_entry(tmp_config, now=now, send_alert=False)
        assert event is not None
        assert event.direction == "long"

        conn = init_db(tmp_config.db_path)
        trades = get_open_trades(conn)
        assert len(trades) == 1
        assert trades[0]["entry_price"] == 95000.0
        conn.close()

    @pytest.mark.asyncio
    async def test_no_event_today(self, tmp_config):
        now = datetime(2026, 5, 7, 10, 3, tzinfo=timezone.utc)
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=95000.0),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            event = await run_entry(tmp_config, now=now, send_alert=False)
        assert event is None

    @pytest.mark.asyncio
    async def test_idempotent_double_entry(self, tmp_config):
        now = datetime(2026, 5, 6, 10, 3, tzinfo=timezone.utc)
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=95000.0),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            await run_entry(tmp_config, now=now, send_alert=False)
            await run_entry(tmp_config, now=now, send_alert=False)

        conn = init_db(tmp_config.db_path)
        rows = conn.execute("SELECT COUNT(*) FROM calendar_trades").fetchone()[0]
        assert rows == 1
        conn.close()

    @pytest.mark.asyncio
    async def test_btc_price_failure(self, tmp_config):
        now = datetime(2026, 5, 6, 10, 3, tzinfo=timezone.utc)
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=None),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            event = await run_entry(tmp_config, now=now, send_alert=False)
        assert event is not None  # event detected

        conn = init_db(tmp_config.db_path)
        trade = get_trade(conn, "2026-05-06", "pre_fomc_long")
        assert trade["status"] == "pending"  # not opened without price
        conn.close()


class TestRunExit:
    @pytest.mark.asyncio
    async def test_exit_after_hold(self, tmp_config):
        conn = init_db(tmp_config.db_path)
        record_signal(conn, "2026-05-06", "pre_fomc_long", "long", 8)
        # Set entry_time explicitly (mark_entry uses datetime.now which breaks
        # when the real date passes the test date)
        conn.execute(
            """UPDATE calendar_trades SET status='open', entry_price=95000.0,
               entry_time='2026-05-06T10:00:00+00:00'
               WHERE event_date='2026-05-06' AND event_type='pre_fomc_long'"""
        )
        conn.commit()
        conn.close()

        now = datetime(2026, 5, 6, 18, 5, tzinfo=timezone.utc)  # 8h+ after 10:00
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=96000.0),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            results = await run_exit(tmp_config, now=now, send_alert=False)

        assert results is not None
        assert len(results) == 1
        expected_pnl = (96000 - 95000) / 95000 * 100
        assert results[0]["pnl"] == pytest.approx(expected_pnl, rel=1e-3)

    @pytest.mark.asyncio
    async def test_no_exit_before_hold(self, tmp_config):
        conn = init_db(tmp_config.db_path)
        record_signal(conn, "2026-05-06", "pre_fomc_long", "long", 8)
        # Manually set entry_time to a controlled value
        conn.execute(
            """UPDATE calendar_trades SET status='open', entry_price=95000.0,
               entry_time='2026-05-06T10:00:00+00:00'
               WHERE event_date='2026-05-06' AND event_type='pre_fomc_long'"""
        )
        conn.commit()
        conn.close()

        now = datetime(2026, 5, 6, 15, 0, tzinfo=timezone.utc)  # 5h into 8h hold
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=96000.0),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            results = await run_exit(tmp_config, now=now, send_alert=False)
        assert results is None

    @pytest.mark.asyncio
    async def test_exit_short_trade(self, tmp_config):
        conn = init_db(tmp_config.db_path)
        record_signal(conn, "2026-06-26", "post_q_expiry_short", "short", 24)
        mark_entry(conn, "2026-06-26", "post_q_expiry_short", 95000.0)
        conn.close()

        now = datetime(2026, 6, 27, 9, 0, tzinfo=timezone.utc)  # 25h after 08:00
        with (
            patch("src.calendar_signal.runner._fetch_btc_price", return_value=94000.0),
            patch("src.calendar_signal.runner._telegram_config", return_value=None),
        ):
            results = await run_exit(tmp_config, now=now, send_alert=False)

        assert results is not None
        assert results[0]["pnl"] > 0  # SHORT, price went down = profit
