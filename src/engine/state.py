"""State manager — in-memory + periodic SQLite persistence."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger()

DB_PATH = Path("data/engine_state.db")


class StateManager:
    """Persist engine state to SQLite. Load on startup, save periodically."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._init_schema()
        self._running = False

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price TEXT,
                qty TEXT,
                pnl TEXT,
                metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def save_positions(self, positions: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("DELETE FROM positions")
        for pos in positions:
            self._conn.execute(
                "INSERT INTO positions (symbol, data, updated_at) VALUES (?, ?, ?)",
                (pos["symbol"], json.dumps(pos), now),
            )
        self._conn.commit()

    def load_positions(self) -> list[dict]:
        rows = self._conn.execute("SELECT data FROM positions").fetchall()
        return [json.loads(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Trade log
    # ------------------------------------------------------------------

    def log_trade(
        self,
        symbol: str,
        strategy_id: str,
        side: str,
        action: str,  # "open" or "close"
        price: str = "",
        qty: str = "",
        pnl: str = "",
        metadata: dict | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO trades_log (timestamp, symbol, strategy_id, side, action, price, qty, pnl, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                symbol, strategy_id, side, action, price, qty, pnl,
                json.dumps(metadata or {}),
            ),
        )
        self._conn.commit()

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT timestamp, symbol, strategy_id, side, action, price, qty, pnl, metadata "
            "FROM trades_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "timestamp": r[0], "symbol": r[1], "strategy_id": r[2],
                "side": r[3], "action": r[4], "price": r[5],
                "qty": r[6], "pnl": r[7], "metadata": json.loads(r[8]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # KV store
    # ------------------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    # ------------------------------------------------------------------
    # Periodic save loop
    # ------------------------------------------------------------------

    async def periodic_save(self, positions_fn, interval: int = 60) -> None:
        """Save positions every `interval` seconds."""
        self._running = True
        while self._running:
            await asyncio.sleep(interval)
            try:
                positions = positions_fn()
                self.save_positions(positions)
            except Exception:
                logger.exception("state_save_error")

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self._conn.close()
