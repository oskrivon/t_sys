"""Integration tests for the weekend signal flow."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from src.weekend.config import DEFAULT_CONFIG, WeekendConfig
from src.weekend.ensemble import EnsembleResult
from src.weekend.runner import run_friday_signal, run_sl_check, run_sunday_settlement
from src.weekend.state import (
    get_open_trade,
    get_stats,
    get_trade_by_date,
    init_db,
    mark_entry,
    mark_exit,
    record_signal,
)


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path):
    """Config with temporary DB path."""
    return WeekendConfig(
        predictors=DEFAULT_CONFIG.predictors,
        majority_threshold=3,
        stop_loss_pct=0.02,
        db_path=str(tmp_path / "test_weekend.db"),
        yfinance_retry_attempts=1,
        yfinance_retry_delay_seconds=0,
    )


@pytest.fixture
def db(tmp_config):
    """Initialized database connection."""
    conn = init_db(tmp_config.db_path)
    yield conn
    conn.close()


def _make_week_df(fri_open=100.0, fri_close=105.0, mon_open=98.0):
    """Build a minimal week DataFrame."""
    rows = [
        {"date": "2026-04-20", "open": mon_open, "close": mon_open + 1},
        {"date": "2026-04-21", "open": 99.0, "close": 100.0},
        {"date": "2026-04-22", "open": 100.0, "close": 101.0},
        {"date": "2026-04-23", "open": 101.0, "close": 102.0},
        {"date": "2026-04-24", "open": fri_open, "close": fri_close},
    ]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["weekday"] = df["date"].dt.weekday
    df["high"] = df["close"]
    df["low"] = df["open"]
    df["volume"] = 1000
    return df


# ── State/DB Tests ─────────────────────────────────────────────────────

class TestStateDB:
    def test_record_and_retrieve(self, db):
        row_id = record_signal(db, "2026-04-24", "long", 3, 5, [{"name": "test"}])
        assert row_id is not None
        trade = get_trade_by_date(db, "2026-04-24")
        assert trade["direction"] == "long"
        assert trade["status"] == "pending"
        assert trade["vote_sum"] == 3

    def test_idempotent_insert(self, db):
        id1 = record_signal(db, "2026-04-24", "long", 3, 5, [])
        id2 = record_signal(db, "2026-04-24", "long", 3, 5, [])
        assert id1 is not None
        assert id2 is None  # duplicate, no-op

    def test_entry_and_exit_lifecycle(self, db):
        record_signal(db, "2026-04-24", "long", 3, 5, [])
        mark_entry(db, "2026-04-24", 100000.0, 98000.0)

        trade = get_open_trade(db)
        assert trade is not None
        assert trade["status"] == "open"
        assert trade["entry_price"] == 100000.0

        mark_exit(db, "2026-04-24", 101000.0, 1.0)
        trade = get_trade_by_date(db, "2026-04-24")
        assert trade["status"] == "closed"
        assert trade["pnl_pct"] == 1.0

    def test_sl_hit_lifecycle(self, db):
        record_signal(db, "2026-04-24", "long", 3, 5, [])
        mark_entry(db, "2026-04-24", 100000.0, 98000.0)
        mark_exit(db, "2026-04-24", 97500.0, -2.5, sl_hit=True)

        trade = get_trade_by_date(db, "2026-04-24")
        assert trade["status"] == "sl_hit"
        assert trade["sl_hit"] == 1

    def test_stats_computation(self, db):
        for i, (d, pnl) in enumerate([
            ("long", 1.5), ("short", -0.8), ("long", 2.0), ("short", -1.0),
        ]):
            date = f"2026-04-{10 + i}"
            record_signal(db, date, d, 3, 5, [])
            mark_entry(db, date, 100000.0, 98000.0)
            mark_exit(db, date, 100000.0 + pnl * 1000, pnl)

        stats = get_stats(db)
        assert stats["total"] == 4
        assert stats["wins"] == 2
        assert stats["losses"] == 2
        assert stats["win_rate"] == 50.0
        assert stats["best"] == 2.0
        assert stats["worst"] == -1.0

    def test_no_open_trade_returns_none(self, db):
        assert get_open_trade(db) is None

    def test_predictor_details_stored_as_json(self, db):
        details = [
            {"name": "china_inet_fri", "ticker": "KWEB", "value_pct": 1.5, "vote": 1},
            {"name": "japan_fri", "ticker": "EWJ", "value_pct": -0.3, "vote": -1},
        ]
        record_signal(db, "2026-04-24", "long", 1, 2, details)
        trade = get_trade_by_date(db, "2026-04-24")
        loaded = json.loads(trade["predictor_details"])
        assert len(loaded) == 2
        assert loaded[0]["name"] == "china_inet_fri"


# ── Runner Integration Tests ──────────────────────────────────────────

class TestFridaySignal:
    @pytest.mark.asyncio
    async def test_happy_path_signal_generated(self, tmp_config):
        """All predictors bullish -> LONG signal recorded in DB."""
        bullish_df = _make_week_df(fri_open=100.0, fri_close=105.0, mon_open=95.0)

        with (
            patch("src.weekend.predictors.fetch_ticker_data", return_value=bullish_df),
            patch("src.weekend.runner._fetch_btc_price", return_value=95000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_friday_signal(
                tmp_config, dry_run=True, send_alert=False,
            )

        assert result.direction == "long"
        assert result.total_votes == 5

        conn = init_db(tmp_config.db_path)
        trade = get_open_trade(conn)
        assert trade is not None
        assert trade["direction"] == "long"
        assert trade["entry_price"] == 95000.0
        conn.close()

    @pytest.mark.asyncio
    async def test_split_vote_no_trade(self, tmp_config):
        """2 bullish + 2 bearish + 1 failed -> no consensus, no trade."""
        from src.weekend.predictors import InsufficientDataError
        call_count = 0

        def split_df(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_week_df(fri_open=100.0, fri_close=105.0, mon_open=95.0)  # bullish
            if call_count <= 4:
                return _make_week_df(fri_open=105.0, fri_close=100.0, mon_open=110.0)  # bearish
            raise InsufficientDataError("test fail")  # 5th fails

        with (
            patch("src.weekend.predictors.fetch_ticker_data", side_effect=split_df),
            patch("src.weekend.runner._fetch_btc_price", return_value=95000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_friday_signal(
                tmp_config, dry_run=True, send_alert=False,
            )

        assert result.direction is None
        assert result.total_votes == 4
        assert len(result.failed_predictors) == 1

        conn = init_db(tmp_config.db_path)
        assert get_open_trade(conn) is None
        conn.close()

    @pytest.mark.asyncio
    async def test_idempotent_double_run(self, tmp_config):
        """Running twice for same date doesn't create duplicate trades."""
        bullish_df = _make_week_df(fri_open=100.0, fri_close=105.0, mon_open=95.0)

        with (
            patch("src.weekend.predictors.fetch_ticker_data", return_value=bullish_df),
            patch("src.weekend.runner._fetch_btc_price", return_value=95000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            await run_friday_signal(tmp_config, dry_run=True, send_alert=False)
            await run_friday_signal(tmp_config, dry_run=True, send_alert=False)

        conn = init_db(tmp_config.db_path)
        rows = conn.execute("SELECT COUNT(*) FROM weekend_trades").fetchone()[0]
        assert rows == 1
        conn.close()

    @pytest.mark.asyncio
    async def test_partial_failure_still_trades(self, tmp_config):
        """2/5 fail, 3 agree -> trade."""
        call_count = 0

        def partial_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return _make_week_df(fri_open=100.0, fri_close=105.0, mon_open=95.0)
            from src.weekend.predictors import InsufficientDataError
            raise InsufficientDataError("test fail")

        with (
            patch("src.weekend.predictors.fetch_ticker_data", side_effect=partial_fail),
            patch("src.weekend.runner._fetch_btc_price", return_value=95000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_friday_signal(
                tmp_config, dry_run=True, send_alert=False,
            )

        assert result.direction == "long"
        assert result.total_votes == 3
        assert len(result.failed_predictors) == 2

    @pytest.mark.asyncio
    async def test_total_failure_no_trade(self, tmp_config):
        """All predictors fail -> no trade."""
        from src.weekend.predictors import InsufficientDataError

        with (
            patch("src.weekend.predictors.fetch_ticker_data",
                  side_effect=InsufficientDataError("down")),
            patch("src.weekend.runner._fetch_btc_price", return_value=95000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_friday_signal(
                tmp_config, dry_run=True, send_alert=False,
            )

        assert result.direction is None
        assert result.total_votes == 0

    @pytest.mark.asyncio
    async def test_btc_price_failure_signal_still_generated(self, tmp_config):
        """BTC price unavailable -> signal computed but not entered in DB."""
        bullish_df = _make_week_df(fri_open=100.0, fri_close=105.0, mon_open=95.0)

        with (
            patch("src.weekend.predictors.fetch_ticker_data", return_value=bullish_df),
            patch("src.weekend.runner._fetch_btc_price", return_value=None),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_friday_signal(
                tmp_config, dry_run=True, send_alert=False,
            )

        assert result.direction == "long"

        conn = init_db(tmp_config.db_path)
        trade = get_trade_by_date(conn, result.date)
        assert trade is not None
        assert trade["status"] == "pending"  # not opened, no price
        conn.close()


class TestSundaySettlement:
    @pytest.mark.asyncio
    async def test_settle_open_trade(self, tmp_config):
        """Open trade gets settled with P&L."""
        conn = init_db(tmp_config.db_path)
        record_signal(conn, "2026-04-24", "long", 3, 5, [])
        mark_entry(conn, "2026-04-24", 95000.0, 93100.0)
        conn.close()

        with (
            patch("src.weekend.runner._fetch_btc_price", return_value=96000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_sunday_settlement(tmp_config, send_alert=False)

        assert result is not None
        expected_pnl = (96000 - 95000) / 95000 * 100
        assert result["pnl"] == pytest.approx(expected_pnl, rel=1e-4)

        conn = init_db(tmp_config.db_path)
        trade = get_trade_by_date(conn, "2026-04-24")
        assert trade["status"] == "closed"
        conn.close()

    @pytest.mark.asyncio
    async def test_settle_no_trade(self, tmp_config):
        """No open trade -> returns None."""
        with (
            patch("src.weekend.runner._fetch_btc_price", return_value=96000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_sunday_settlement(tmp_config, send_alert=False)
        assert result is None


class TestSLCheck:
    @pytest.mark.asyncio
    async def test_sl_hit(self, tmp_config):
        conn = init_db(tmp_config.db_path)
        record_signal(conn, "2026-04-24", "long", 3, 5, [])
        mark_entry(conn, "2026-04-24", 95000.0, 93100.0)
        conn.close()

        with (
            patch("src.weekend.runner._fetch_btc_price", return_value=93000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_sl_check(tmp_config, send_alert=False)

        assert result is not None
        assert result["sl_hit"] is True
        assert result["pnl"] < 0

    @pytest.mark.asyncio
    async def test_sl_not_hit(self, tmp_config):
        conn = init_db(tmp_config.db_path)
        record_signal(conn, "2026-04-24", "long", 3, 5, [])
        mark_entry(conn, "2026-04-24", 95000.0, 93100.0)
        conn.close()

        with (
            patch("src.weekend.runner._fetch_btc_price", return_value=96000.0),
            patch("src.weekend.runner._telegram_config", return_value=None),
        ):
            result = await run_sl_check(tmp_config, send_alert=False)

        assert result is None  # no SL hit

        conn = init_db(tmp_config.db_path)
        trade = get_open_trade(conn)
        assert trade["status"] == "open"
        conn.close()


class TestConfigValidation:
    def test_threshold_exceeds_predictors(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            WeekendConfig(
                predictors=DEFAULT_CONFIG.predictors[:2],
                majority_threshold=5,
            )

    def test_threshold_zero(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            WeekendConfig(
                predictors=DEFAULT_CONFIG.predictors,
                majority_threshold=0,
            )

    def test_sl_out_of_range(self):
        with pytest.raises(ValueError, match="between 0 and 1"):
            WeekendConfig(
                predictors=DEFAULT_CONFIG.predictors,
                stop_loss_pct=2.0,
            )

    def test_valid_config(self):
        cfg = WeekendConfig(
            predictors=DEFAULT_CONFIG.predictors,
            majority_threshold=3,
            stop_loss_pct=0.02,
        )
        assert cfg.majority_threshold == 3
