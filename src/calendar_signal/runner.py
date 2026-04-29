"""Calendar signal orchestrator — entry, exit, SL check."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import structlog

from src.weekend.alerter import send_telegram
from src.weekend.ensemble import compute_pnl, compute_sl_price, is_sl_hit

from src.calendar_signal.config import CalendarConfig, EventDef
from src.calendar_signal.events import (
    event_to_dict,
    get_entry_event,
    should_exit,
)
from src.calendar_signal.state import (
    get_open_trades,
    get_stats,
    init_db,
    mark_entry,
    mark_exit,
    record_signal,
)

logger = structlog.get_logger()


def _fetch_btc_price(symbol: str) -> float | None:
    try:
        import ccxt
        exchange = ccxt.bybit()
        ticker = exchange.fetch_ticker(symbol)
        return ticker["last"]
    except Exception as e:
        logger.error("btc_price_failed", error=str(e))
        return None


def _telegram_config() -> tuple[str, str] | None:
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return None


def _format_entry_message(event: EventDef, btc_price: float | None, sl_price: float | None) -> str:
    lines = [
        "=" * 40,
        "CALENDAR SIGNAL — ENTRY",
        f"Event: {event.event_type.value}",
        f"Date: {event.date}",
        "=" * 40,
        "",
        f"Direction: {event.direction.upper()}",
        f"Hold: {event.hold_hours}h",
    ]
    if btc_price is not None:
        lines.append(f"BTC: ${btc_price:,.0f}")
    if sl_price is not None:
        lines.append(f"SL: ${sl_price:,.0f}")
    lines.extend(["", event.description, "=" * 40])
    return "\n".join(lines)


def _format_exit_message(
    event_type: str, direction: str, entry_price: float,
    exit_price: float, pnl: float, sl_hit: bool, stats: dict,
) -> str:
    lines = [
        "=" * 40,
        "CALENDAR TRADE — EXIT",
        f"Event: {event_type}",
        "=" * 40,
        "",
        f"Direction: {direction.upper()}",
        f"Entry: ${entry_price:,.0f}",
        f"Exit: ${exit_price:,.0f}",
        f"P&L: {pnl:+.2f}%{'  (SL HIT)' if sl_hit else ''}",
    ]
    if stats.get("total", 0) > 0:
        lines.extend([
            "",
            f"Cumulative ({stats['total']} trades):",
            f"  WR: {stats['win_rate']:.0f}%  Total P&L: {stats['total_pnl']:+.1f}%",
        ])
    lines.append("=" * 40)
    return "\n".join(lines)


async def run_entry(
    config: CalendarConfig,
    now: datetime | None = None,
    send_alert: bool = True,
) -> EventDef | None:
    """Check if there's an event to enter right now. If so, enter."""
    if now is None:
        now = datetime.now(timezone.utc)

    event = get_entry_event(config, now)
    if event is None:
        logger.info("calendar_no_entry", date=now.strftime("%Y-%m-%d %H:%M"))
        return None

    logger.info("calendar_entry_triggered",
                event_type=event.event_type.value, date=event.date)

    conn = init_db(config.db_path)
    try:
        row_id = record_signal(
            conn, event.date, event.event_type.value,
            event.direction, event.hold_hours,
        )

        btc_price = _fetch_btc_price(config.target_symbol)
        sl_price = None

        if btc_price is not None:
            if config.stop_loss_pct > 0:
                sl_price = compute_sl_price(btc_price, event.direction, config.stop_loss_pct)
            mark_entry(conn, event.date, event.event_type.value, btc_price, sl_price)

        message = _format_entry_message(event, btc_price, sl_price)
        print(message)

        if send_alert:
            tg = _telegram_config()
            if tg:
                await send_telegram(message, tg[0], tg[1])

        return event
    finally:
        conn.close()


async def run_exit(
    config: CalendarConfig,
    now: datetime | None = None,
    send_alert: bool = True,
) -> list[dict] | None:
    """Check open trades and exit if hold period expired."""
    if now is None:
        now = datetime.now(timezone.utc)

    conn = init_db(config.db_path)
    try:
        open_trades = get_open_trades(conn)
        if not open_trades:
            return None

        results = []
        for trade in open_trades:
            # Reconstruct EventDef to check hold
            event = EventDef(
                event_type=trade["event_type"],
                date=trade["event_date"],
                direction=trade["direction"],
                entry_hour_utc=0,  # not needed for exit check
                hold_hours=trade["hold_hours"],
            )

            if not should_exit(event, trade["entry_time"], now):
                continue

            btc_price = _fetch_btc_price(config.target_symbol)
            if btc_price is None:
                logger.error("calendar_exit_no_price", event_date=trade["event_date"])
                continue

            pnl = compute_pnl(trade["entry_price"], btc_price, trade["direction"])
            mark_exit(conn, trade["id"], btc_price, pnl)
            stats = get_stats(conn)

            logger.info("calendar_trade_closed",
                        event_type=trade["event_type"], pnl=f"{pnl:+.2f}%")

            message = _format_exit_message(
                trade["event_type"], trade["direction"],
                trade["entry_price"], btc_price, pnl, False, stats,
            )
            print(message)

            if send_alert:
                tg = _telegram_config()
                if tg:
                    await send_telegram(message, tg[0], tg[1])

            results.append({"event_type": trade["event_type"], "pnl": pnl})

        return results if results else None
    finally:
        conn.close()


async def run_sl_check(
    config: CalendarConfig,
    send_alert: bool = True,
) -> list[dict] | None:
    """Check SL on open trades."""
    if config.stop_loss_pct <= 0:
        return None

    conn = init_db(config.db_path)
    try:
        open_trades = get_open_trades(conn)
        if not open_trades:
            return None

        btc_price = _fetch_btc_price(config.target_symbol)
        if btc_price is None:
            return None

        results = []
        for trade in open_trades:
            if trade["sl_price"] is None:
                continue

            if is_sl_hit(btc_price, trade["sl_price"], trade["direction"]):
                pnl = compute_pnl(trade["entry_price"], btc_price, trade["direction"])
                mark_exit(conn, trade["id"], btc_price, pnl, sl_hit=True)
                stats = get_stats(conn)

                logger.warning("calendar_sl_hit",
                               event_type=trade["event_type"], pnl=f"{pnl:+.2f}%")

                message = _format_exit_message(
                    trade["event_type"], trade["direction"],
                    trade["entry_price"], btc_price, pnl, True, stats,
                )
                print(message)

                if send_alert:
                    tg = _telegram_config()
                    if tg:
                        await send_telegram(message, tg[0], tg[1])

                results.append({"event_type": trade["event_type"], "pnl": pnl, "sl_hit": True})

        return results if results else None
    finally:
        conn.close()
