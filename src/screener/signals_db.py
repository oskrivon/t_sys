"""SQLite persistence for screener signals — queryable trade history."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    rr_ratio REAL,
    level_price REAL,
    level_touches INTEGER,
    level_score INTEGER,
    ml_score REAL,
    vision_score INTEGER,
    volume_ratio REAL,
    exit_price REAL,
    exit_time TEXT,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
"""


def init_signals_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_signal(
    conn: sqlite3.Connection,
    timestamp: str,
    symbol: str,
    signal_type: str,
    direction: str,
    timeframe: str,
    entry_price: float,
    sl: float,
    tp: float,
    rr_ratio: float = 0,
    level_price: float = 0,
    level_touches: int = 0,
    level_score: int = 0,
    ml_score: Optional[float] = None,
    vision_score: Optional[int] = None,
    volume_ratio: Optional[float] = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO signals
           (timestamp, symbol, signal_type, direction, timeframe,
            entry_price, sl, tp, rr_ratio,
            level_price, level_touches, level_score,
            ml_score, vision_score, volume_ratio,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (timestamp, symbol, signal_type, direction, timeframe,
         entry_price, sl, tp, rr_ratio,
         level_price, level_touches, level_score,
         ml_score, vision_score, volume_ratio, now),
    )
    conn.commit()
    log.info("signal_recorded", symbol=symbol, signal_type=signal_type,
             direction=direction, id=cur.lastrowid)
    return cur.lastrowid


def close_signal(
    conn: sqlite3.Connection,
    signal_id: int,
    exit_price: float,
    pnl_pct: float,
    status: str = "closed",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE signals SET exit_price=?, exit_time=?, pnl_pct=?, status=?
           WHERE id=?""",
        (exit_price, now, pnl_pct, status, signal_id),
    )
    conn.commit()


def get_open_signals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE status='open' ORDER BY timestamp DESC"
    ).fetchall()


def get_signals_history(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()


def get_signals_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT pnl_pct, direction FROM signals WHERE status IN ('closed', 'sl_hit', 'tp_hit')"
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
        "win_rate": len(wins) / len(pnls) * 100,
        "avg_pnl": sum(pnls) / len(pnls),
        "total_pnl": sum(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
    }
