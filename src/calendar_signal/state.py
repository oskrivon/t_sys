"""SQLite persistence for calendar trades."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TEXT,
    exit_time TEXT,
    entry_price REAL,
    exit_price REAL,
    sl_price REAL,
    sl_hit INTEGER DEFAULT 0,
    pnl_pct REAL,
    hold_hours INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE(event_date, event_type)
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def record_signal(
    conn: sqlite3.Connection,
    event_date: str,
    event_type: str,
    direction: str,
    hold_hours: int,
) -> int | None:
    """Record a new signal. Returns row id, or None if already exists."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = conn.execute(
            """INSERT INTO calendar_trades
               (event_date, event_type, direction, hold_hours, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (event_date, event_type, direction, hold_hours, now),
        )
        conn.commit()
        logger.info("calendar_signal_recorded",
                     event_date=event_date, event_type=event_type, direction=direction)
        return cur.lastrowid
    except sqlite3.IntegrityError:
        logger.info("calendar_signal_exists", event_date=event_date, event_type=event_type)
        return None


def mark_entry(
    conn: sqlite3.Connection,
    event_date: str,
    event_type: str,
    entry_price: float,
    sl_price: float | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE calendar_trades
           SET status='open', entry_price=?, sl_price=?, entry_time=?
           WHERE event_date=? AND event_type=? AND status='pending'""",
        (entry_price, sl_price, now, event_date, event_type),
    )
    conn.commit()


def mark_exit(
    conn: sqlite3.Connection,
    trade_id: int,
    exit_price: float,
    pnl_pct: float,
    sl_hit: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    status = "sl_hit" if sl_hit else "closed"
    conn.execute(
        """UPDATE calendar_trades
           SET status=?, exit_price=?, pnl_pct=?, sl_hit=?, exit_time=?
           WHERE id=? AND status='open'""",
        (status, exit_price, pnl_pct, int(sl_hit), now, trade_id),
    )
    conn.commit()


def get_open_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM calendar_trades WHERE status='open' ORDER BY id"
    )
    return cur.fetchall()


def get_trade(conn: sqlite3.Connection, event_date: str, event_type: str) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM calendar_trades WHERE event_date=? AND event_type=?",
        (event_date, event_type),
    )
    return cur.fetchone()


def get_history(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM calendar_trades ORDER BY event_date DESC, event_type LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT pnl_pct, sl_hit, event_type FROM calendar_trades WHERE status IN ('closed', 'sl_hit')"
    ).fetchall()
    if not rows:
        return {"total": 0}
    pnls = [r["pnl_pct"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "total": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
        "avg_pnl": sum(pnls) / len(pnls),
        "total_pnl": sum(pnls),
        "best": max(pnls),
        "worst": min(pnls),
    }
