"""Paper trade tracker — saves signals, resolves outcomes via price checks."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.strategy.models import Signal
from . import db

log = structlog.get_logger()


class PaperTrader:
    """Tracks paper trades in SQLite.

    Usage:
        trader = PaperTrader()
        trader.record_signal(signal)         # called from screener
        await trader.check_open_trades(exchange)  # called periodically
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or db.DEFAULT_DB_PATH
        db.init_db(self.db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = db.get_connection(self.db_path)
        return self._conn

    def record_signal(self, signal: Signal) -> int:
        """Save a signal as an open paper trade. Returns trade id."""
        ts = signal.timestamp.isoformat() if signal.timestamp else datetime.now(timezone.utc).isoformat()
        direction = "long" if signal.is_long else "short"

        trade_id = db.insert_trade(
            self.conn,
            signal_time=ts,
            symbol=signal.symbol,
            signal_type=signal.signal_type.value,
            direction=direction,
            entry_price=signal.entry_price,
            sl=signal.sl,
            tp=signal.tp,
            rr_ratio=signal.rr_ratio,
            level_price=signal.level.price,
            level_touches=signal.level.touches,
            level_score=signal.level.score,
            ml_score=signal.ml_score,
            vision_score=signal.vision_score,
            volume_ratio=signal.volume_ratio,
        )
        log.info(
            "paper_trade_opened",
            id=trade_id,
            symbol=signal.symbol,
            direction=direction,
            entry=signal.entry_price,
            sl=signal.sl,
            tp=signal.tp,
        )
        return trade_id

    async def check_open_trades(self, exchange) -> list[dict]:
        """Check current prices for all open trades, close those that hit TP/SL.

        Returns list of resolved trades with outcomes.
        """
        open_trades = db.get_open_trades(self.conn)
        if not open_trades:
            return []

        # Collect unique symbols
        symbols = list({t["symbol"] for t in open_trades})

        # Fetch current prices
        prices = {}
        for symbol in symbols:
            try:
                ticker = await exchange.fetch_ticker(symbol)
                prices[symbol] = {
                    "last": ticker["last"],
                    "high": ticker.get("high"),
                    "low": ticker.get("low"),
                }
            except Exception as e:
                log.warning("price_fetch_error", symbol=symbol, error=str(e))

        resolved = []
        for trade in open_trades:
            price_info = prices.get(trade["symbol"])
            if not price_info:
                continue

            result = self._check_trade(trade, price_info)
            if result:
                resolved.append(result)

        return resolved

    def check_open_trades_with_prices(self, price_map: dict[str, float]) -> list[dict]:
        """Check open trades against provided prices (sync version).

        price_map: {symbol: current_price}
        """
        open_trades = db.get_open_trades(self.conn)
        resolved = []
        for trade in open_trades:
            price = price_map.get(trade["symbol"])
            if price is None:
                continue
            price_info = {"last": price, "high": price, "low": price}
            result = self._check_trade(trade, price_info)
            if result:
                resolved.append(result)
        return resolved

    def _check_trade(self, trade: sqlite3.Row, price_info: dict) -> Optional[dict]:
        """Check if a trade hit TP or SL. Returns outcome dict or None."""
        last = price_info["last"]
        # Use high/low for intra-candle TP/SL detection
        high = price_info.get("high") or last
        low = price_info.get("low") or last

        entry = trade["entry_price"]
        tp = trade["tp"]
        sl = trade["sl"]
        is_long = trade["direction"] == "long"

        status = None
        close_price = last

        if is_long:
            if low <= sl:
                status = "sl_hit"
                close_price = sl
            elif high >= tp:
                status = "tp_hit"
                close_price = tp
        else:
            if high >= sl:
                status = "sl_hit"
                close_price = sl
            elif low <= tp:
                status = "tp_hit"
                close_price = tp

        if status is None:
            return None

        if is_long:
            pnl_pct = (close_price - entry) / entry * 100
        else:
            pnl_pct = (entry - close_price) / entry * 100

        db.close_trade(
            self.conn,
            trade["id"],
            status=status,
            close_price=close_price,
            pnl_pct=pnl_pct,
        )

        result = {
            "id": trade["id"],
            "symbol": trade["symbol"],
            "direction": trade["direction"],
            "entry": entry,
            "close_price": close_price,
            "status": status,
            "pnl_pct": round(pnl_pct, 2),
        }
        log.info("paper_trade_closed", **result)
        return result

    def get_open_count(self) -> int:
        return len(db.get_open_trades(self.conn))

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
