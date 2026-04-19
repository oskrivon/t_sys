"""SQLite database for paper trading.

Lightweight, zero-config. One file in data/paper_trades.db.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()

DEFAULT_DB_PATH = Path("data/paper_trades.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    rr_ratio REAL NOT NULL,
    level_price REAL NOT NULL,
    level_touches INTEGER,
    level_score INTEGER,
    ml_score REAL,
    vision_score INTEGER,
    volume_ratio REAL,
    status TEXT NOT NULL DEFAULT 'open',
    close_price REAL,
    close_time TEXT,
    pnl_pct REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Get a connection with row factory enabled."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create tables if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.close()
    log.info("paper_trading_db_init", path=str(db_path))


def insert_trade(
    conn: sqlite3.Connection,
    *,
    signal_time: str,
    symbol: str,
    signal_type: str,
    direction: str,
    entry_price: float,
    sl: float,
    tp: float,
    rr_ratio: float,
    level_price: float,
    level_touches: Optional[int] = None,
    level_score: Optional[int] = None,
    ml_score: Optional[float] = None,
    vision_score: Optional[int] = None,
    volume_ratio: Optional[float] = None,
) -> int:
    """Insert a new paper trade. Returns the trade id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO trades (
            signal_time, symbol, signal_type, direction,
            entry_price, sl, tp, rr_ratio,
            level_price, level_touches, level_score,
            ml_score, vision_score, volume_ratio,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            signal_time, symbol, signal_type, direction,
            entry_price, sl, tp, rr_ratio,
            level_price, level_touches, level_score,
            ml_score, vision_score, volume_ratio,
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def close_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    status: str,
    close_price: float,
    pnl_pct: float,
) -> None:
    """Close a trade with outcome."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE trades
        SET status = ?, close_price = ?, close_time = ?, pnl_pct = ?
        WHERE id = ?
        """,
        (status, close_price, now, pnl_pct, trade_id),
    )
    conn.commit()


def get_open_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get all open trades."""
    return conn.execute(
        "SELECT * FROM trades WHERE status = 'open' ORDER BY signal_time"
    ).fetchall()


def get_all_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get all trades ordered by signal time descending."""
    return conn.execute(
        "SELECT * FROM trades ORDER BY signal_time DESC"
    ).fetchall()


def get_closed_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get closed trades (tp_hit, sl_hit, expired)."""
    return conn.execute(
        "SELECT * FROM trades WHERE status != 'open' ORDER BY signal_time DESC"
    ).fetchall()
