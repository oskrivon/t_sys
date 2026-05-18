"""SQLite persistence for weekend trades."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekend_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL UNIQUE,
    direction TEXT NOT NULL,
    entry_time TEXT,
    exit_time TEXT,
    entry_price REAL,
    exit_price REAL,
    sl_price REAL,
    sl_hit INTEGER DEFAULT 0,
    pnl_pct REAL,
    reversal_price REAL,
    reversal_time TEXT,
    vote_sum INTEGER NOT NULL,
    total_votes INTEGER NOT NULL,
    predictor_details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize database and create table if needed."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    # Migrate: add reversal columns if missing (for existing DBs)
    try:
        conn.execute("ALTER TABLE weekend_trades ADD COLUMN reversal_price REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE weekend_trades ADD COLUMN reversal_time TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def record_signal(
    conn: sqlite3.Connection,
    signal_date: str,
    direction: str,
    vote_sum: int,
    total_votes: int,
    predictor_details: list[dict],
) -> int | None:
    """Record a new signal. Returns row id, or None if already exists (idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    details_json = json.dumps(predictor_details, ensure_ascii=False)
    try:
        cur = conn.execute(
            """INSERT INTO weekend_trades
               (signal_date, direction, vote_sum, total_votes, predictor_details, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (signal_date, direction, vote_sum, total_votes, details_json, now),
        )
        conn.commit()
        logger.info("signal_recorded", signal_date=signal_date, direction=direction, id=cur.lastrowid)
        return cur.lastrowid
    except sqlite3.IntegrityError:
        logger.info("signal_already_exists", signal_date=signal_date)
        return None


def mark_entry(
    conn: sqlite3.Connection,
    signal_date: str,
    entry_price: float,
    sl_price: float,
) -> None:
    """Mark trade as open with entry price and SL."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE weekend_trades
           SET status='open', entry_price=?, sl_price=?, entry_time=?
           WHERE signal_date=? AND status='pending'""",
        (entry_price, sl_price, now, signal_date),
    )
    conn.commit()


def mark_reversal(
    conn: sqlite3.Connection,
    signal_date: str,
    reversal_price: float,
    new_direction: str,
    new_sl_price: float,
) -> None:
    """Record mid-weekend reversal: update direction, set new SL, store reversal price."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE weekend_trades
           SET direction=?, sl_price=?, reversal_price=?, reversal_time=?
           WHERE signal_date=? AND status='open'""",
        (new_direction, new_sl_price, reversal_price, now, signal_date),
    )
    conn.commit()


def mark_exit(
    conn: sqlite3.Connection,
    signal_date: str,
    exit_price: float,
    pnl_pct: float,
    sl_hit: bool = False,
) -> None:
    """Close a trade with exit price and P&L."""
    now = datetime.now(timezone.utc).isoformat()
    status = "sl_hit" if sl_hit else "closed"
    conn.execute(
        """UPDATE weekend_trades
           SET status=?, exit_price=?, pnl_pct=?, sl_hit=?, exit_time=?
           WHERE signal_date=? AND status='open'""",
        (status, exit_price, pnl_pct, int(sl_hit), now, signal_date),
    )
    conn.commit()


def get_open_trade(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Get the currently open trade, if any."""
    cur = conn.execute(
        "SELECT * FROM weekend_trades WHERE status='open' ORDER BY id DESC LIMIT 1"
    )
    return cur.fetchone()


def get_pending_trade(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Get a pending (not yet entered) trade."""
    cur = conn.execute(
        "SELECT * FROM weekend_trades WHERE status='pending' ORDER BY id DESC LIMIT 1"
    )
    return cur.fetchone()


def get_trade_by_date(conn: sqlite3.Connection, signal_date: str) -> Optional[sqlite3.Row]:
    """Get trade for a specific Friday date."""
    cur = conn.execute(
        "SELECT * FROM weekend_trades WHERE signal_date=?", (signal_date,)
    )
    return cur.fetchone()


def get_history(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Get recent trade history."""
    cur = conn.execute(
        "SELECT * FROM weekend_trades ORDER BY signal_date DESC LIMIT ?", (limit,)
    )
    return cur.fetchall()


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute aggregate stats from closed trades."""
    rows = conn.execute(
        "SELECT pnl_pct, sl_hit FROM weekend_trades WHERE status IN ('closed', 'sl_hit')"
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
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "best": max(pnls),
        "worst": min(pnls),
        "sl_count": sum(1 for r in rows if r["sl_hit"]),
    }
