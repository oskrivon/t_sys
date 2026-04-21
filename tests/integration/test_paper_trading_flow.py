"""Integration tests: Paper trading DB flow.

Tests the full lifecycle of paper trades through the SQLite database:
insert -> query -> close -> verify.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.paper_trading.db import SCHEMA, insert_trade, get_open_trades, close_trade, get_all_trades


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn():
    """In-memory SQLite connection with schema initialized."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _insert_sample_trade(conn, symbol="BTC/USDT:USDT", direction="long", entry_price=50000.0):
    """Helper to insert a sample trade with required fields."""
    return insert_trade(
        conn,
        signal_time="2026-04-20T12:00:00Z",
        symbol=symbol,
        signal_type="breakout",
        direction=direction,
        entry_price=entry_price,
        sl=49000.0,
        tp=52000.0,
        rr_ratio=2.0,
        level_price=50100.0,
        level_touches=3,
        level_score=5,
        ml_score=0.75,
        vision_score=7,
        volume_ratio=1.5,
    )


# ---------------------------------------------------------------------------
# Insert and query
# ---------------------------------------------------------------------------

class TestInsertAndQuery:
    def test_insert_returns_trade_id(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        assert isinstance(trade_id, int)
        assert trade_id > 0

    def test_inserted_trade_appears_in_open_trades(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        open_trades = get_open_trades(db_conn)

        assert len(open_trades) == 1
        assert open_trades[0]["id"] == trade_id
        assert open_trades[0]["symbol"] == "BTC/USDT:USDT"
        assert open_trades[0]["status"] == "open"

    def test_multiple_inserts_all_open(self, db_conn):
        _insert_sample_trade(db_conn, symbol="BTC/USDT:USDT")
        _insert_sample_trade(db_conn, symbol="ETH/USDT:USDT", entry_price=3000.0)
        _insert_sample_trade(db_conn, symbol="SOL/USDT:USDT", entry_price=150.0)

        open_trades = get_open_trades(db_conn)
        assert len(open_trades) == 3

    def test_insert_stores_all_fields(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        open_trades = get_open_trades(db_conn)
        t = open_trades[0]

        assert t["signal_time"] == "2026-04-20T12:00:00Z"
        assert t["signal_type"] == "breakout"
        assert t["direction"] == "long"
        assert t["entry_price"] == 50000.0
        assert t["sl"] == 49000.0
        assert t["tp"] == 52000.0
        assert t["rr_ratio"] == 2.0
        assert t["level_price"] == 50100.0
        assert t["level_touches"] == 3
        assert t["level_score"] == 5
        assert t["ml_score"] == 0.75
        assert t["vision_score"] == 7
        assert t["volume_ratio"] == 1.5

    def test_insert_with_optional_fields_none(self, db_conn):
        trade_id = insert_trade(
            db_conn,
            signal_time="2026-04-20T12:00:00Z",
            symbol="DOGE/USDT:USDT",
            signal_type="retest",
            direction="short",
            entry_price=0.15,
            sl=0.16,
            tp=0.13,
            rr_ratio=1.5,
            level_price=0.15,
        )
        open_trades = get_open_trades(db_conn)
        t = open_trades[0]

        assert t["ml_score"] is None
        assert t["vision_score"] is None
        assert t["level_touches"] is None


# ---------------------------------------------------------------------------
# Close trade
# ---------------------------------------------------------------------------

class TestCloseTrade:
    def test_close_trade_updates_status(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        close_trade(db_conn, trade_id, status="tp_hit", close_price=52000.0, pnl_pct=4.0)

        open_trades = get_open_trades(db_conn)
        assert len(open_trades) == 0

        all_trades = get_all_trades(db_conn)
        assert len(all_trades) == 1
        assert all_trades[0]["status"] == "tp_hit"

    def test_close_trade_stores_pnl(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        close_trade(db_conn, trade_id, status="tp_hit", close_price=52000.0, pnl_pct=4.0)

        all_trades = get_all_trades(db_conn)
        t = all_trades[0]
        assert t["pnl_pct"] == 4.0
        assert t["close_price"] == 52000.0
        assert t["close_time"] is not None

    def test_close_trade_sl_hit(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        close_trade(db_conn, trade_id, status="sl_hit", close_price=49000.0, pnl_pct=-2.0)

        all_trades = get_all_trades(db_conn)
        assert all_trades[0]["status"] == "sl_hit"
        assert all_trades[0]["pnl_pct"] == -2.0

    def test_close_only_target_trade(self, db_conn):
        id1 = _insert_sample_trade(db_conn, symbol="BTC/USDT:USDT")
        id2 = _insert_sample_trade(db_conn, symbol="ETH/USDT:USDT")

        close_trade(db_conn, id1, status="tp_hit", close_price=52000.0, pnl_pct=4.0)

        open_trades = get_open_trades(db_conn)
        assert len(open_trades) == 1
        assert open_trades[0]["id"] == id2
        assert open_trades[0]["symbol"] == "ETH/USDT:USDT"


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_insert_get_close_verify(self, db_conn):
        """Complete cycle: insert -> query open -> close -> verify closed state."""
        # 1. Insert
        trade_id = _insert_sample_trade(db_conn, symbol="SOL/USDT:USDT", entry_price=150.0)

        # 2. Verify open
        open_trades = get_open_trades(db_conn)
        assert len(open_trades) == 1
        assert open_trades[0]["symbol"] == "SOL/USDT:USDT"

        # 3. Close with profit
        close_trade(
            db_conn, trade_id,
            status="tp_hit",
            close_price=160.0,
            pnl_pct=6.67,
        )

        # 4. Verify closed
        open_trades = get_open_trades(db_conn)
        assert len(open_trades) == 0

        all_trades = get_all_trades(db_conn)
        assert len(all_trades) == 1
        t = all_trades[0]
        assert t["status"] == "tp_hit"
        assert t["close_price"] == 160.0
        assert abs(t["pnl_pct"] - 6.67) < 0.01

    def test_multiple_trades_mixed_outcomes(self, db_conn):
        """Multiple trades with different outcomes."""
        id1 = _insert_sample_trade(db_conn, symbol="BTC/USDT:USDT")
        id2 = _insert_sample_trade(db_conn, symbol="ETH/USDT:USDT", entry_price=3000.0)
        id3 = _insert_sample_trade(db_conn, symbol="SOL/USDT:USDT", entry_price=150.0)

        # Close two, leave one open
        close_trade(db_conn, id1, status="tp_hit", close_price=52000.0, pnl_pct=4.0)
        close_trade(db_conn, id2, status="sl_hit", close_price=2800.0, pnl_pct=-6.67)

        open_trades = get_open_trades(db_conn)
        assert len(open_trades) == 1
        assert open_trades[0]["id"] == id3

        all_trades = get_all_trades(db_conn)
        assert len(all_trades) == 3

    def test_expired_trade(self, db_conn):
        trade_id = _insert_sample_trade(db_conn)
        close_trade(db_conn, trade_id, status="expired", close_price=50100.0, pnl_pct=0.2)

        all_trades = get_all_trades(db_conn)
        assert all_trades[0]["status"] == "expired"
