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
) -> EnsembleResult:
    """Friday flow: fetch data -> compute ensemble -> record -> alert.

    Args:
        config: Weekend strategy configuration.
        dry_run: Use last Friday's data (no day-of-week check).
        historical: Specific date "YYYY-MM-DD" to compute for.
        send_alert: Whether to send Telegram alert.

    Returns:
        EnsembleResult with signal details.
    """
    target = _get_target_friday(dry_run=dry_run, historical=historical)
    signal_date = target.strftime("%Y-%m-%d")

    # Validate it's Friday (unless dry_run/historical)
    if not dry_run and not historical and target.weekday() != config.entry_weekday:
        logger.warning("not_friday", weekday=target.weekday())

    logger.info("friday_signal_start", date=signal_date, dry_run=dry_run)

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

            if btc_price is not None:
                sl_price = compute_sl_price(
                    btc_price, ensemble.direction, config.stop_loss_pct,
                )
                mark_entry(conn, signal_date, btc_price, sl_price)
        finally:
            conn.close()

    # Format and send alert
    message = format_signal_message(ensemble, btc_price, sl_price, config)
    print(message)

    if send_alert:
        tg = _telegram_config()
        if tg:
            await send_telegram(message, tg[0], tg[1])

    return ensemble


async def run_sunday_settlement(
    config: WeekendConfig,
    send_alert: bool = True,
) -> dict | None:
    """Sunday flow: close open trade, compute P&L, alert."""
    conn = init_db(config.db_path)
    try:
        trade = get_open_trade(conn)
        if not trade:
            logger.info("no_open_trade")
            return None

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
) -> dict | None:
    """Check if SL hit on open trade. Close early if so."""
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

        if is_sl_hit(btc_price, trade["sl_price"], trade["direction"]):
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
