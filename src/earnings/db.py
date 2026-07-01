"""SQLite persistence for earnings backtest data."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

from .models import EarningsEvent, TranscriptScore

logger = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    earnings_date TEXT NOT NULL,
    report_time TEXT,
    eps_estimate REAL,
    eps_actual REAL,
    eps_surprise_pct REAL,
    revenue_estimate REAL,
    revenue_actual REAL,
    revenue_surprise_pct REAL,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, earnings_date)
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    earnings_date TEXT NOT NULL,
    quarter TEXT NOT NULL,
    year INTEGER NOT NULL,
    transcript_text TEXT,
    word_count INTEGER,
    fetched_at TEXT NOT NULL,
    UNIQUE(symbol, earnings_date)
);

CREATE TABLE IF NOT EXISTS llm_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    earnings_date TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    sentiment INTEGER,
    guidance_direction INTEGER,
    management_confidence INTEGER,
    risk_flags TEXT,
    key_themes TEXT,
    composite_score REAL,
    reasoning TEXT,
    scored_at TEXT NOT NULL,
    UNIQUE(symbol, earnings_date, model)
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    earnings_date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    eps_surprise_pct REAL,
    llm_composite_score REAL,
    entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL,
    hold_days INTEGER NOT NULL,
    exit_price REAL NOT NULL,
    exit_time TEXT NOT NULL,
    pnl_pct REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize database and create tables if needed."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_event(conn: sqlite3.Connection, event: EarningsEvent) -> None:
    """Insert or update an earnings event."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO earnings_events
           (symbol, earnings_date, report_time, eps_estimate, eps_actual,
            eps_surprise_pct, revenue_estimate, revenue_actual,
            revenue_surprise_pct, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, earnings_date)
           DO UPDATE SET
             eps_estimate=excluded.eps_estimate,
             eps_actual=excluded.eps_actual,
             eps_surprise_pct=excluded.eps_surprise_pct,
             revenue_estimate=excluded.revenue_estimate,
             revenue_actual=excluded.revenue_actual,
             revenue_surprise_pct=excluded.revenue_surprise_pct
        """,
        (
            event.symbol,
            event.earnings_date.isoformat(),
            event.report_time,
            event.eps_estimate,
            event.eps_actual,
            event.eps_surprise_pct,
            event.revenue_estimate,
            event.revenue_actual,
            event.revenue_surprise_pct,
            now,
        ),
    )
    conn.commit()


def upsert_events_batch(
    conn: sqlite3.Connection, events: list[EarningsEvent]
) -> int:
    """Batch insert/update earnings events. Returns count inserted."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            e.symbol, e.earnings_date.isoformat(), e.report_time,
            e.eps_estimate, e.eps_actual, e.eps_surprise_pct,
            e.revenue_estimate, e.revenue_actual, e.revenue_surprise_pct,
            now,
        )
        for e in events
    ]
    conn.executemany(
        """INSERT INTO earnings_events
           (symbol, earnings_date, report_time, eps_estimate, eps_actual,
            eps_surprise_pct, revenue_estimate, revenue_actual,
            revenue_surprise_pct, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, earnings_date) DO UPDATE SET
             eps_estimate=excluded.eps_estimate,
             eps_actual=excluded.eps_actual,
             eps_surprise_pct=excluded.eps_surprise_pct
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def get_events(
    conn: sqlite3.Connection,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    symbol: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch earnings events with optional filters."""
    query = "SELECT * FROM earnings_events WHERE 1=1"
    params: list[Any] = []
    if start_date:
        query += " AND earnings_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND earnings_date <= ?"
        params.append(end_date)
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY earnings_date, symbol"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def save_llm_score(
    conn: sqlite3.Connection,
    symbol: str,
    earnings_date: str,
    model: str,
    prompt_hash: str,
    raw_response: str,
    score: TranscriptScore,
) -> None:
    """Save LLM scoring result."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO llm_scores
           (symbol, earnings_date, model, prompt_hash, raw_response,
            sentiment, guidance_direction, management_confidence,
            risk_flags, key_themes, composite_score, reasoning, scored_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, earnings_date, model) DO UPDATE SET
             prompt_hash=excluded.prompt_hash,
             raw_response=excluded.raw_response,
             sentiment=excluded.sentiment,
             guidance_direction=excluded.guidance_direction,
             management_confidence=excluded.management_confidence,
             risk_flags=excluded.risk_flags,
             key_themes=excluded.key_themes,
             composite_score=excluded.composite_score,
             reasoning=excluded.reasoning,
             scored_at=excluded.scored_at
        """,
        (
            symbol, earnings_date, model, prompt_hash, raw_response,
            score.sentiment, score.guidance_direction,
            score.management_confidence,
            json.dumps(score.risk_flags),
            json.dumps(score.key_themes),
            score.composite_score, score.reasoning, now,
        ),
    )
    conn.commit()


def get_llm_scores(
    conn: sqlite3.Connection,
    model: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch all LLM scores, optionally filtered by model."""
    query = "SELECT * FROM llm_scores"
    params: list[Any] = []
    if model:
        query += " WHERE model = ?"
        params.append(model)
    return [dict(r) for r in conn.execute(query, params).fetchall()]
