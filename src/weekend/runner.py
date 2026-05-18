"""Weekend signal orchestrator — Friday signal, Sunday settlement, SL check."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import structlog

from src.weekend.alerter import (
    format_settlement_message,
    format_signal_message,
    send_telegram,
)
from src.weekend.config import WeekendConfig
from src.weekend.ensemble import (
    EnsembleResult,
    compute_ensemble,
    compute_pnl,
    compute_sl_price,
    is_sl_hit,
)
from src.weekend.predictors import compute_predictor
from src.weekend.state import (
    get_open_trade,
    get_pending_trade,
    get_stats,
    get_trade_by_date,
    init_db,
    mark_entry,
    mark_exit,
    record_signal,
)

logger = structlog.get_logger()


def _get_target_friday(dry_run: bool = False, historical: str | None = None) -> datetime:
    """Determine which Friday to compute signal for."""
    now = datetime.now(timezone.utc)

    if historical:
        return datetime.strptime(historical, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if dry_run:
        days_since_friday = (now.weekday() - 4) % 7
        if days_since_friday == 0 and now.hour < 21:
            days_since_friday = 7
        return (now - timedelta(days=days_since_friday)).replace(
            hour=21, minute=0, second=0, microsecond=0,
        )

    return now


def _fetch_btc_price(symbol: str) -> float | None:
    """Fetch current BTC price from Bybit."""
    try:
        import ccxt
        exchange = ccxt.bybit()
        ticker = exchange.fetch_ticker(symbol)
        return ticker["last"]
    except Exception as e:
        logger.error("btc_price_failed", error=str(e))
        return None


def _telegram_config() -> tuple[str, str] | None:
    """Load Telegram config from env."""
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return None


async def run_friday_signal(
    config: WeekendConfig,
    dry_run: bool = False,
    historical: str | None = None,
    send_alert: bool = True,
    live: bool = False,
) -> EnsembleResult:
    """Friday flow: fetch data -> compute ensemble -> record -> alert -> [live: open positions].

    Args:
        config: Weekend strategy configuration.
        dry_run: Use last Friday's data (no day-of-week check).
        historical: Specific date "YYYY-MM-DD" to compute for.
        send_alert: Whether to send Telegram alert.
        live: Open real positions on exchanges.

    Returns:
        EnsembleResult with signal details.
    """
    target = _get_target_friday(dry_run=dry_run, historical=historical)
    signal_date = target.strftime("%Y-%m-%d")

    # Validate it's Friday (unless dry_run/historical)
    if not dry_run and not historical and target.weekday() != config.entry_weekday:
        logger.warning("not_friday", weekday=target.weekday())

    logger.info("friday_signal_start", date=signal_date, dry_run=dry_run, live=live)

    import pandas as pd
    target_ts = pd.Timestamp(target)

    # Compute all predictors
    results = []
    for pred in config.predictors:
        result = compute_predictor(
            pred, target_ts,
            retries=config.yfinance_retry_attempts,
            retry_delay=config.yfinance_retry_delay_seconds,
        )
        results.append(result)

    # Ensemble vote
    predictor_names = [p.name for p in config.predictors]
    ensemble = compute_ensemble(
        results, predictor_names, config.majority_threshold, signal_date,
    )

    logger.info(
        "ensemble_computed",
        date=signal_date,
        direction=ensemble.direction,
        vote_sum=ensemble.vote_sum,
        total_votes=ensemble.total_votes,
        failed=list(ensemble.failed_predictors),
    )

    # Fetch BTC price
    btc_price = _fetch_btc_price(config.target_symbol)
    sl_price = None

    # Record to DB and compute SL
    already_open = False
    if ensemble.direction:
        conn = init_db(config.db_path)
        try:
            # Predictor details for JSON storage
            details = [
                {"name": pr.name, "ticker": pr.ticker,
                 "value_pct": pr.value_pct, "vote": pr.vote}
                for pr in ensemble.predictors
            ]

            row_id = record_signal(
                conn, signal_date, ensemble.direction,
                ensemble.vote_sum, ensemble.total_votes, details,
            )

            if row_id is None:
                # Signal already recorded — check if positions already open
                existing = get_trade_by_date(conn, signal_date)
                if existing and existing["status"] in ("open", "closed", "sl_hit"):
                    already_open = True
                    logger.info("weekend_already_opened", date=signal_date,
                                status=existing["status"])

            if btc_price is not None and not already_open:
                sl_price = compute_sl_price(
                    btc_price, ensemble.direction, config.stop_loss_pct,
                )
                mark_entry(conn, signal_date, btc_price, sl_price)
        finally:
            conn.close()

        # Live execution: open positions on exchanges
        if live and btc_price is not None and sl_price is not None and not already_open:
            from src.weekend.executor import open_position

            fills = []
            for exch in config.exchanges:
                fill = open_position(
                    exchange_id=exch,
                    symbol=config.target_symbol,
                    direction=ensemble.direction,
                    notional=config.notional_per_exchange,
                    leverage=config.leverage,
                    sl_price=sl_price,
                    margin_reserve_pct=config.margin_reserve_pct,
                )
                if fill:
                    fills.append(fill)

            if fills:
                live_lines = ["\n** LIVE POSITIONS OPENED **"]
                for f in fills:
                    live_lines.append(
                        f"  {f.exchange}: {f.side} {f.qty} @ ${f.avg_price:,.1f}"
                        f"  SL order: {f.sl_order_id or 'FAILED'}"
                    )
                live_msg = "\n".join(live_lines)
                logger.info("weekend_live_opened",
                            exchanges=[f.exchange for f in fills],
                            total_notional=sum(f.qty * f.avg_price for f in fills))
            else:
                live_msg = "\n** LIVE: ALL EXCHANGES FAILED TO OPEN **"
                logger.error("weekend_live_all_failed")

    # Format and send alert
    message = format_signal_message(ensemble, btc_price, sl_price, config)
    if live and ensemble.direction:
        if already_open:
            message += "\n** RETRY: positions already open, skipping **"
        else:
            message += live_msg
    print(message)

    if send_alert:
        tg = _telegram_config()
        if tg:
            await send_telegram(message, tg[0], tg[1])

    return ensemble


async def run_sunday_settlement(
    config: WeekendConfig,
    send_alert: bool = True,
    live: bool = False,
) -> dict | None:
    """Sunday flow: close open trade, compute P&L, alert. [live: close exchange positions]."""
    conn = init_db(config.db_path)
    try:
        trade = get_open_trade(conn)
        if not trade:
            logger.info("no_open_trade")
            return None

        # Live: close positions on exchanges first
        live_msg = ""
        if live:
            from src.weekend.executor import close_position

            fills = []
            for exch in config.exchanges:
                fill = close_position(exch, config.target_symbol, trade["direction"])
                if fill:
                    fills.append(fill)

            if fills:
                live_lines = ["\n** LIVE POSITIONS CLOSED **"]
                for f in fills:
                    live_lines.append(
                        f"  {f.exchange}: {f.side} {f.qty} @ ${f.avg_price:,.1f}"
                    )
                live_msg = "\n".join(live_lines)
            else:
                live_msg = "\n** LIVE: no positions found on exchanges **"

        btc_price = _fetch_btc_price(config.target_symbol)
        if btc_price is None:
            logger.error("settlement_no_price")
            return None

        pnl = compute_pnl(trade["entry_price"], btc_price, trade["direction"])
        mark_exit(conn, trade["signal_date"], btc_price, pnl, sl_hit=False)
        stats = get_stats(conn)

        logger.info(
            "trade_settled",
            date=trade["signal_date"],
            direction=trade["direction"],
            pnl=f"{pnl:+.2f}%",
        )

        message = format_settlement_message(
            trade["signal_date"], trade["direction"],
            trade["entry_price"], btc_price, pnl, False, stats,
        )
        if live:
            message += live_msg
        print(message)

        if send_alert:
            tg = _telegram_config()
            if tg:
                await send_telegram(message, tg[0], tg[1])

        return {"signal_date": trade["signal_date"], "pnl": pnl, "stats": stats}
    finally:
        conn.close()


async def run_sl_check(
    config: WeekendConfig,
    send_alert: bool = True,
    live: bool = False,
) -> dict | None:
    """Check if SL hit on open trade. Close early if so.

    In live mode, exchange SL orders handle the stop — this is a backup check.
    If SL triggered on exchange, positions are already closed; we just update DB.
    """
    conn = init_db(config.db_path)
    try:
        trade = get_open_trade(conn)
        if not trade:
            return None

        if trade["sl_price"] is None:
            logger.warning("no_sl_price", date=trade["signal_date"])
            return None

        btc_price = _fetch_btc_price(config.target_symbol)
        if btc_price is None:
            return None

        # In live mode, also verify exchange positions
        if live:
            from src.weekend.executor import check_position, close_position

            any_open = False
            for exch in config.exchanges:
                pos = check_position(exch, config.target_symbol)
                if pos.get("qty", 0) > 0:
                    any_open = True
                    logger.info("sl_check_live_position",
                                exchange=exch, qty=pos["qty"],
                                upnl=pos.get("upnl"))

            # If no positions on exchanges but DB says open → SL triggered on exchange
            if not any_open:
                logger.warning("sl_triggered_on_exchange",
                               date=trade["signal_date"])
                pnl = compute_pnl(trade["entry_price"], btc_price, trade["direction"])
                mark_exit(conn, trade["signal_date"], btc_price, pnl, sl_hit=True)
                stats = get_stats(conn)

                message = format_settlement_message(
                    trade["signal_date"], trade["direction"],
                    trade["entry_price"], btc_price, pnl, True, stats,
                )
                message += "\n** SL triggered on exchange (positions already closed) **"
                print(message)

                if send_alert:
                    tg = _telegram_config()
                    if tg:
                        await send_telegram(message, tg[0], tg[1])

                return {"signal_date": trade["signal_date"], "pnl": pnl, "sl_hit": True}

        if is_sl_hit(btc_price, trade["sl_price"], trade["direction"]):
            # Live: close remaining positions (backup, exchange SL should have handled it)
            if live:
                for exch in config.exchanges:
                    close_position(exch, config.target_symbol, trade["direction"])

            pnl = compute_pnl(trade["entry_price"], btc_price, trade["direction"])
            mark_exit(conn, trade["signal_date"], btc_price, pnl, sl_hit=True)
            stats = get_stats(conn)

            logger.warning(
                "sl_hit",
                date=trade["signal_date"],
                price=btc_price,
                sl=trade["sl_price"],
                pnl=f"{pnl:+.2f}%",
            )

            message = format_settlement_message(
                trade["signal_date"], trade["direction"],
                trade["entry_price"], btc_price, pnl, True, stats,
            )
            print(message)

            if send_alert:
                tg = _telegram_config()
                if tg:
                    await send_telegram(message, tg[0], tg[1])

            return {"signal_date": trade["signal_date"], "pnl": pnl, "sl_hit": True}

        logger.info(
            "sl_check_ok",
            date=trade["signal_date"],
            price=btc_price,
            sl=trade["sl_price"],
        )
        return None
    finally:
        conn.close()
