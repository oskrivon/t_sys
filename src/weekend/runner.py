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
    mark_reentry,
    mark_reversal,
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
                # Paper mode: virtual fill, mark open immediately.
                # Live mode: defer mark_entry until an exchange actually fills,
                # otherwise a failed open leaves a phantom 'open' trade and the
                # retry crons skip it (positions already open).
                if not live:
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
                # At least one exchange filled — now record the entry. Use the
                # real average fill price across legs instead of the pre-fetch price.
                fill_price = sum(f.avg_price for f in fills) / len(fills)
                conn = init_db(config.db_path)
                try:
                    mark_entry(conn, signal_date, fill_price, sl_price)
                finally:
                    conn.close()

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
                # No fills — leave status 'pending' so the retry crons re-attempt.
                live_msg = "\n** LIVE: ALL EXCHANGES FAILED TO OPEN — left PENDING for retry **"
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

        # Compute P&L based on trade path
        if trade["reentry_price"] is not None:
            # SL hit + re-entry: leg1 (entry->SL) + leg2 (reentry->exit)
            sl_exit = trade["sl_exit_price"] or trade["entry_price"]
            leg1 = compute_pnl(trade["entry_price"], sl_exit, trade["direction"])
            leg2 = compute_pnl(trade["reentry_price"], btc_price, trade["direction"])
            pnl = leg1 + leg2
            logger.info("settlement_reentry_trade",
                        leg1=f"{leg1:+.2f}%", leg2=f"{leg2:+.2f}%", total=f"{pnl:+.2f}%")
        elif trade["sl_hit"] and trade["sl_exit_price"]:
            # SL hit but no re-entry (bounce never came) — just SL loss
            pnl = compute_pnl(trade["entry_price"], trade["sl_exit_price"], trade["direction"])
            logger.info("settlement_sl_only", pnl=f"{pnl:+.2f}%")
        elif trade["reversal_price"] is not None:
            # Mid-weekend reversal: leg1 (entry->reversal) + leg2 (reversal->exit)
            orig_dir = "short" if trade["direction"] == "long" else "long"
            leg1 = compute_pnl(trade["entry_price"], trade["reversal_price"], orig_dir)
            leg2 = compute_pnl(trade["reversal_price"], btc_price, trade["direction"])
            pnl = leg1 + leg2
            logger.info("settlement_reversed_trade",
                        leg1=f"{leg1:+.2f}%", leg2=f"{leg2:+.2f}%", total=f"{pnl:+.2f}%")
        else:
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
    """Catastrophe SL check — closes trade permanently if 5% SL hit.

    No re-entry logic: backtest shows hold-to-settlement is optimal,
    this SL exists only as a safety net for black swan events.
    """
    conn = init_db(config.db_path)
    try:
        trade = get_open_trade(conn)
        if not trade:
            return None

        btc_price = _fetch_btc_price(config.target_symbol)
        if btc_price is None:
            return None

        # If trade already hit SL (legacy state), nothing to do
        if trade["sl_hit"]:
            return None

        if trade["sl_price"] is None:
            logger.warning("no_sl_price", date=trade["signal_date"])
            return None

        # ── Check if catastrophe SL hit ──
        sl_hit_detected = False

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

            if not any_open:
                logger.warning("sl_triggered_on_exchange",
                               date=trade["signal_date"])
                sl_hit_detected = True

        if not sl_hit_detected and is_sl_hit(btc_price, trade["sl_price"], trade["direction"]):
            if live:
                from src.weekend.executor import close_position
                for exch in config.exchanges:
                    close_position(exch, config.target_symbol, trade["direction"])
            sl_hit_detected = True

        if sl_hit_detected:
            sl_pnl = -config.stop_loss_pct * 100
            # Mark as terminal SL hit — no re-entry
            from src.weekend.ensemble import compute_pnl
            actual_pnl = compute_pnl(trade["entry_price"], btc_price, trade["direction"])
            mark_exit(conn, trade["signal_date"], btc_price, actual_pnl, sl_hit=True)

            logger.warning(
                "catastrophe_sl_hit",
                date=trade["signal_date"],
                price=btc_price,
                sl=trade["sl_price"],
                pnl=f"{actual_pnl:+.2f}%",
            )

            message = (
                f"========================================\n"
                f"WEEKEND CATASTROPHE SL HIT\n"
                f"Date: {trade['signal_date']}\n"
                f"========================================\n"
                f"\n"
                f"Direction: {trade['direction'].upper()}\n"
                f"Entry: ${trade['entry_price']:,.0f}\n"
                f"Exit: ${btc_price:,.0f}\n"
                f"P&L: {actual_pnl:+.2f}%\n"
                f"\n"
                f"Trade closed. No re-entry.\n"
                f"========================================\n"
            )
            print(message)

            if send_alert:
                tg = _telegram_config()
                if tg:
                    await send_telegram(message, tg[0], tg[1])

            return {"signal_date": trade["signal_date"],
                    "pnl": actual_pnl, "sl_hit": True}

        logger.info(
            "sl_check_ok",
            date=trade["signal_date"],
            price=btc_price,
            sl=trade["sl_price"],
        )
        print(f"OK -- no SL hit")
        return None
    finally:
        conn.close()


async def _check_reentry_bounce(
    conn,
    config: WeekendConfig,
    trade,
    btc_price: float,
    send_alert: bool,
    live: bool,
) -> dict | None:
    """After SL hit: check if price has bounced enough to re-enter.

    Bounce = price moved back in original direction by reentry_bounce_pct
    from sl_exit_price (the local extreme at/after SL).
    """
    sl_exit_price = trade["sl_exit_price"]
    if sl_exit_price is None:
        logger.warning("no_sl_exit_price", date=trade["signal_date"])
        return None

    direction = trade["direction"]
    entry_price = trade["entry_price"]

    # Check if already re-entered
    if trade["reentry_price"] is not None:
        # Already re-entered, check re-entry SL
        re_sl = trade["reentry_sl_price"]
        if re_sl and is_sl_hit(btc_price, re_sl, direction):
            # Re-entry SL hit — close trade for good
            if live:
                from src.weekend.executor import close_position
                for exch in config.exchanges:
                    close_position(exch, config.target_symbol, direction)

            # Total PnL: original SL + re-entry loss
            orig_sl_pnl = compute_pnl(entry_price, sl_exit_price, direction)
            re_pnl = -config.reentry_sl_pct * 100
            total_pnl = orig_sl_pnl + re_pnl
            mark_exit(conn, trade["signal_date"], btc_price, total_pnl, sl_hit=True)

            logger.warning("reentry_sl_hit", date=trade["signal_date"],
                           price=btc_price, total_pnl=f"{total_pnl:+.2f}%")

            stats = get_stats(conn)
            message = format_settlement_message(
                trade["signal_date"], direction,
                entry_price, btc_price, total_pnl, True, stats,
            )
            message += "\n** Re-entry SL hit **"
            print(message)

            if send_alert:
                tg = _telegram_config()
                if tg:
                    await send_telegram(message, tg[0], tg[1])

            return {"signal_date": trade["signal_date"], "pnl": total_pnl,
                    "sl_hit": True, "reentry_sl_hit": True}

        logger.info("reentry_sl_check_ok", date=trade["signal_date"],
                    price=btc_price, re_sl=re_sl)
        return None

    # Check time limit: don't re-enter too late
    if trade["entry_time"]:
        entry_time = datetime.fromisoformat(trade["entry_time"])
        hours_since_entry = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
        max_reentry_h = config.reentry_max_hours + (
            (sl_exit_price - entry_price) / entry_price * 100  # approximate SL hour
            if direction == "long" else 0
        )
        # Simple check: entry + SL + bounce must be within weekend window
        if hours_since_entry > 40:  # too close to Sunday exit
            logger.info("reentry_too_late", date=trade["signal_date"],
                        hours=hours_since_entry)
            # Close trade with just the SL loss
            pnl = compute_pnl(entry_price, sl_exit_price, direction)
            mark_exit(conn, trade["signal_date"], sl_exit_price, pnl, sl_hit=True)

            stats = get_stats(conn)
            message = format_settlement_message(
                trade["signal_date"], direction,
                entry_price, sl_exit_price, pnl, True, stats,
            )
            message += "\n** Re-entry window expired, SL loss final **"
            print(message)

            if send_alert:
                tg = _telegram_config()
                if tg:
                    await send_telegram(message, tg[0], tg[1])

            return {"signal_date": trade["signal_date"], "pnl": pnl,
                    "sl_hit": True, "reentry_expired": True}

    # Check bounce: has price recovered enough from SL exit?
    if direction == "long":
        bounce = (btc_price - sl_exit_price) / sl_exit_price
    else:
        bounce = (sl_exit_price - btc_price) / sl_exit_price

    logger.info("reentry_bounce_check", date=trade["signal_date"],
                direction=direction, price=btc_price,
                sl_exit=sl_exit_price,
                bounce_pct=f"{bounce*100:+.2f}%",
                threshold=f"{config.reentry_bounce_pct*100:.1f}%")

    if bounce < config.reentry_bounce_pct:
        # Update sl_exit_price if price moved further against us (track local extreme)
        if direction == "long" and btc_price < sl_exit_price:
            conn.execute(
                "UPDATE weekend_trades SET sl_exit_price=? WHERE signal_date=? AND status='open'",
                (btc_price, trade["signal_date"]),
            )
            conn.commit()
            logger.info("reentry_local_low_updated", price=btc_price)
        elif direction == "short" and btc_price > sl_exit_price:
            conn.execute(
                "UPDATE weekend_trades SET sl_exit_price=? WHERE signal_date=? AND status='open'",
                (btc_price, trade["signal_date"]),
            )
            conn.commit()
            logger.info("reentry_local_high_updated", price=btc_price)

        print(f"OK -- bounce {bounce*100:+.2f}%, need {config.reentry_bounce_pct*100:.1f}%")
        return None

    # ── Bounce detected! Re-enter ──
    reentry_sl = compute_sl_price(btc_price, direction, config.reentry_sl_pct)

    logger.warning(
        "reentry_triggered",
        date=trade["signal_date"],
        direction=direction,
        price=btc_price,
        bounce=f"{bounce*100:+.2f}%",
        reentry_sl=reentry_sl,
    )

    if live:
        from src.weekend.executor import open_position

        for exch in config.exchanges:
            fill = open_position(
                exchange_id=exch,
                symbol=config.target_symbol,
                direction=direction,
                notional=config.notional_per_exchange,
                leverage=config.leverage,
                sl_price=reentry_sl,
                margin_reserve_pct=config.margin_reserve_pct,
            )
            if fill:
                logger.info("reentry_filled", exchange=exch,
                            price=fill.avg_price, qty=fill.qty)

    mark_reentry(conn, trade["signal_date"], btc_price, reentry_sl)

    message = (
        f"========================================\n"
        f"WEEKEND RE-ENTRY\n"
        f"Date: {trade['signal_date']}\n"
        f"========================================\n"
        f"\n"
        f"Direction: {direction.upper()} (same as original)\n"
        f"Original entry: ${entry_price:,.0f}\n"
        f"SL exit: ${sl_exit_price:,.0f}\n"
        f"Re-entry: ${btc_price:,.0f}\n"
        f"Bounce: {bounce*100:+.2f}%\n"
        f"New SL ({config.reentry_sl_pct*100:.0f}%): ${reentry_sl:,.0f}\n"
        f"========================================\n"
    )
    print(message)

    if send_alert:
        tg = _telegram_config()
        if tg:
            await send_telegram(message, tg[0], tg[1])

    return {"signal_date": trade["signal_date"],
            "reentry": True, "price": btc_price, "bounce": bounce * 100}


async def run_reversal_check(
    config: WeekendConfig,
    send_alert: bool = True,
    live: bool = False,
) -> dict | None:
    """DEPRECATED: reversal is subsumed by V-bottom re-entry.

    Analysis (2026-05-23): re-entry triggers before reversal in 33/34 cases.
    The 1 reversal-only case was harmful (-4.83%). Keeping as no-op for
    backwards compatibility with existing cron jobs.
    """
    logger.info("reversal_check_skipped_deprecated")
    print("OK -- reversal check disabled (subsumed by re-entry)")
    return None
