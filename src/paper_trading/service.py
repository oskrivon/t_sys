"""Paper Trading Service — standalone daemon.

Subscribes to Redis signals, records paper trades, checks TP/SL periodically.
Replaces direct PaperTrader coupling in screener.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.core.redis_bus import (
    RedisBus, CH_SIGNALS_SCREENER, CH_SIGNALS_STRATEGY,
    CH_EVENTS_TRADES, CH_NOTIFICATIONS_TG,
)
from src.core.models.signals import ScreenerSignal
from src.paper_trading import db
from src.paper_trading.stats import compute_stats

logger = structlog.get_logger()


class PaperTradingService:
    """Listens for signals via Redis, records and tracks paper trades."""

    def __init__(self, redis_url: str, db_path: Path = db.DEFAULT_DB_PATH) -> None:
        self._bus = RedisBus(redis_url)
        self._db_path = db_path
        self._conn = None
        self._check_interval = 300  # check TP/SL every 5 min

    async def start(self) -> None:
        # Init DB
        db.init_db(self._db_path)
        self._conn = db.get_connection(self._db_path)
        logger.info("paper_trading_db_ready", path=str(self._db_path))

        # Connect Redis
        await self._bus.connect()

        # Subscribe to signals from screener and engine strategies
        self._bus.on(CH_SIGNALS_SCREENER, self._on_screener_signal)
        self._bus.on(CH_SIGNALS_STRATEGY, self._on_strategy_signal)
        self._bus.on(CH_EVENTS_TRADES, self._on_trade_event)

        # Run listener + periodic TP/SL checker
        logger.info("paper_trading_service_started")
        await asyncio.gather(
            self._bus.run(),
            self._check_loop(),
        )

    async def _on_screener_signal(self, message: dict) -> None:
        """Record a screener signal as paper trade."""
        try:
            signal = ScreenerSignal.from_redis(message)
            trade_id = db.insert_trade(
                self._conn,
                signal_time=signal.timestamp.isoformat(),
                symbol=signal.symbol,
                signal_type=signal.signal_type.value,
                direction=signal.direction.value,
                entry_price=signal.entry_price,
                sl=signal.sl,
                tp=signal.tp,
                rr_ratio=signal.rr_ratio,
                level_price=signal.level_price,
                level_touches=signal.level_touches,
                level_score=int(signal.level_score),
                ml_score=signal.ml_score,
                vision_score=int(signal.vision_score) if signal.vision_score else None,
                volume_ratio=signal.volume_ratio,
            )
            logger.info("paper_trade_recorded",
                         id=trade_id,
                         symbol=signal.symbol,
                         direction=signal.direction.value,
                         source=message.get("source", ""))
        except Exception:
            logger.exception("paper_trade_record_error")

    async def _on_strategy_signal(self, message: dict) -> None:
        """Record an engine strategy signal as paper trade."""
        try:
            payload = message.get("payload", {})
            db.insert_trade(
                self._conn,
                signal_time=message.get("timestamp", datetime.now(timezone.utc).isoformat()),
                symbol=payload.get("symbol", ""),
                signal_type=payload.get("signal_type", "unknown"),
                direction=payload.get("direction", "long"),
                entry_price=payload.get("entry_price", 0),
                sl=payload.get("sl", 0),
                tp=payload.get("tp", 0),
                rr_ratio=payload.get("rr_ratio", 3.0),
                level_price=payload.get("level_price", 0),
            )
        except Exception:
            logger.exception("paper_strategy_record_error")

    async def _on_trade_event(self, message: dict) -> None:
        """Log real trade events for comparison with paper results."""
        payload = message.get("payload", {})
        logger.info("real_trade_event",
                     event=payload.get("event"),
                     symbol=payload.get("symbol"),
                     pnl=payload.get("pnl_usd"))

    async def _check_loop(self) -> None:
        """Periodically check open trades for TP/SL hits."""
        import ccxt.async_support as ccxt
        exchange = ccxt.bybit({"enableRateLimit": True})

        while True:
            await asyncio.sleep(self._check_interval)
            try:
                open_trades = db.get_open_trades(self._conn)
                if not open_trades:
                    continue

                symbols = list({t["symbol"] for t in open_trades})
                prices = {}
                for sym in symbols:
                    try:
                        ticker = await exchange.fetch_ticker(sym)
                        prices[sym] = {
                            "last": ticker["last"],
                            "high": ticker.get("high") or ticker["last"],
                            "low": ticker.get("low") or ticker["last"],
                        }
                    except Exception:
                        pass

                resolved = 0
                for trade in open_trades:
                    price_info = prices.get(trade["symbol"])
                    if not price_info:
                        continue
                    result = self._check_trade(trade, price_info)
                    if result:
                        resolved += 1
                        await self._bus.notify(
                            f"PAPER {result['status'].upper()}: {result['symbol']} "
                            f"{result['direction']} PnL={result['pnl_pct']:+.1f}%",
                            source="paper_trading",
                        )

                if resolved:
                    logger.info("paper_trades_checked",
                                open=len(open_trades), resolved=resolved)
            except Exception:
                logger.exception("paper_check_error")

    def _check_trade(self, trade, price_info: dict) -> dict | None:
        """Check if trade hit TP or SL."""
        last = price_info["last"]
        high = price_info.get("high", last)
        low = price_info.get("low", last)
        entry = trade["entry_price"]
        tp = trade["tp"]
        sl = trade["sl"]
        is_long = trade["direction"] == "long"

        status = None
        close_price = last

        if is_long:
            if low <= sl:
                status, close_price = "sl_hit", sl
            elif high >= tp:
                status, close_price = "tp_hit", tp
        else:
            if high >= sl:
                status, close_price = "sl_hit", sl
            elif low <= tp:
                status, close_price = "tp_hit", tp

        if not status:
            return None

        pnl_pct = ((close_price - entry) / entry * 100) if is_long else ((entry - close_price) / entry * 100)

        db.close_trade(self._conn, trade["id"],
                        status=status, close_price=close_price, pnl_pct=pnl_pct)

        return {
            "id": trade["id"], "symbol": trade["symbol"],
            "direction": trade["direction"], "status": status,
            "pnl_pct": round(pnl_pct, 2),
        }

    async def shutdown(self) -> None:
        await self._bus.stop()
        await self._bus.disconnect()
        if self._conn:
            self._conn.close()
