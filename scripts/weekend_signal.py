#!/usr/bin/env python3
"""Weekend Ensemble Signal — VOTE(BABA fri + QQQ fri + XLK week) → BTC.

Run Friday ~21:05 UTC after NYSE close.
Fetches BABA/QQQ/XLK prices, computes ensemble vote, sends Telegram alert.

Usage:
    python scripts/weekend_signal.py            # print to console
    python scripts/weekend_signal.py --telegram  # send alert
    python scripts/weekend_signal.py --dry-run   # show what would happen last Friday

Backtest: 91 trades over 5 years, WR 64%, avg +1.0%/trade,
Sharpe 2.82, MaxDD -8.6%, profitable 6/6 years.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def get_signals(dry_run: bool = False) -> dict:
    """Fetch BABA/QQQ/XLK and compute ensemble signal.

    Returns dict with signal details or None if no trade.
    """
    import yfinance as yf

    now = datetime.now(timezone.utc)

    if dry_run:
        # Find last Friday
        days_since_friday = (now.weekday() - 4) % 7
        if days_since_friday == 0 and now.hour < 21:
            days_since_friday = 7
        target_friday = (now - timedelta(days=days_since_friday)).replace(
            hour=21, minute=0, second=0, microsecond=0
        )
    else:
        # Must be Friday after 20:30 UTC (NYSE closed at 20:00 UTC summer / 21:00 winter)
        if now.weekday() != 4:
            return {"error": f"Today is {now.strftime('%A')}, not Friday"}
        if now.hour < 20:
            return {"error": f"NYSE not closed yet (current {now.hour}:00 UTC)"}
        target_friday = now

    # Fetch data: need this week's Mon-Fri
    start_date = (target_friday - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = (target_friday + timedelta(days=1)).strftime("%Y-%m-%d")

    tickers_data = {}
    for ticker in ["BABA", "QQQ", "XLK"]:
        df = yf.download(ticker, start=start_date, end=end_date,
                         interval="1d", progress=False)
        if len(df) == 0:
            return {"error": f"No data for {ticker}"}
        df = df.reset_index()
        df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
        df["date"] = df["date"].dt.tz_localize(None)
        tickers_data[ticker] = df

    # Compute signals
    signals = {}

    # BABA Friday return
    baba = tickers_data["BABA"]
    baba_fri = baba[baba["date"].dt.weekday == 4]
    if len(baba_fri) == 0:
        return {"error": "No BABA Friday data"}
    baba_last = baba_fri.iloc[-1]
    baba_fri_ret = (baba_last["close"] - baba_last["open"]) / baba_last["open"] * 100
    signals["baba_fri"] = baba_fri_ret

    # QQQ Friday return
    qqq = tickers_data["QQQ"]
    qqq_fri = qqq[qqq["date"].dt.weekday == 4]
    if len(qqq_fri) == 0:
        return {"error": "No QQQ Friday data"}
    qqq_last = qqq_fri.iloc[-1]
    qqq_fri_ret = (qqq_last["close"] - qqq_last["open"]) / qqq_last["open"] * 100
    signals["qqq_fri"] = qqq_fri_ret

    # XLK Week return (Monday open -> Friday close)
    xlk = tickers_data["XLK"]
    xlk_mon = xlk[xlk["date"].dt.weekday == 0]
    xlk_fri = xlk[xlk["date"].dt.weekday == 4]
    if len(xlk_mon) == 0 or len(xlk_fri) == 0:
        return {"error": "No XLK week data"}
    xlk_week_ret = (xlk_fri.iloc[-1]["close"] - xlk_mon.iloc[-1]["open"]) / xlk_mon.iloc[-1]["open"] * 100
    signals["xlk_week"] = xlk_week_ret

    # Votes
    votes = {
        "BABA fri": 1 if baba_fri_ret > 0 else -1,
        "QQQ fri": 1 if qqq_fri_ret > 0 else -1,
        "XLK week": 1 if xlk_week_ret > 0 else -1,
    }

    vote_sum = sum(votes.values())

    if vote_sum >= 2:
        direction = "LONG"
        consensus = vote_sum
    elif vote_sum <= -2:
        direction = "SHORT"
        consensus = abs(vote_sum)
    else:
        direction = None
        consensus = 0

    # BTC current price (for reference)
    btc_price = None
    try:
        import ccxt
        exchange = ccxt.bybit()
        ticker_info = exchange.fetch_ticker("BTC/USDT:USDT")
        btc_price = ticker_info["last"]
    except Exception:
        pass

    return {
        "date": target_friday.strftime("%Y-%m-%d"),
        "signals": signals,
        "votes": votes,
        "vote_sum": vote_sum,
        "direction": direction,
        "consensus": consensus,
        "btc_price": btc_price,
        "entry_time": "Friday 21:00 UTC",
        "exit_time": "Sunday 23:00 UTC",
        "stop_loss": "2%",
    }


def format_message(result: dict) -> str:
    """Format signal as readable message."""
    if "error" in result:
        return f"Weekend Signal Error: {result['error']}"

    lines = []
    lines.append("=" * 40)
    lines.append("WEEKEND ENSEMBLE SIGNAL")
    lines.append(f"Date: {result['date']}")
    lines.append("=" * 40)
    lines.append("")

    # Predictors
    signals = result["signals"]
    votes = result["votes"]
    lines.append("Predictors:")
    for name, vote in votes.items():
        key = name.lower().replace(" ", "_")
        ret = signals.get(key, 0)
        arrow = "UP" if vote > 0 else "DOWN"
        lines.append(f"  {name:>10s}: {ret:+.2f}% -> {arrow}")

    lines.append("")
    lines.append(f"Vote: {result['vote_sum']:+d}/3")
    lines.append("")

    if result["direction"]:
        emoji_dir = "LONG (buy)" if result["direction"] == "LONG" else "SHORT (sell)"
        lines.append(f"SIGNAL: {emoji_dir} BTC")
        lines.append(f"Consensus: {result['consensus']}/3")
        if result["btc_price"]:
            lines.append(f"BTC price: ${result['btc_price']:,.0f}")
            sl_pct = 0.02
            if result["direction"] == "LONG":
                sl_price = result["btc_price"] * (1 - sl_pct)
            else:
                sl_price = result["btc_price"] * (1 + sl_pct)
            lines.append(f"Stop-loss (2%): ${sl_price:,.0f}")
        lines.append(f"Entry: {result['entry_time']}")
        lines.append(f"Exit: {result['exit_time']}")
    else:
        lines.append("NO TRADE - no consensus (split vote)")

    lines.append("")
    lines.append("Backtest: WR 64%, Sharpe 2.82, 6/6 years profitable")
    lines.append("=" * 40)

    return "\n".join(lines)


async def send_telegram(text: str):
    """Send message to Telegram."""
    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return

    from telegram import Bot
    bot = Bot(token=token)
    await bot.send_message(chat_id=chat_id, text=text)
    print("Signal sent to Telegram")


def main():
    parser = argparse.ArgumentParser(description="Weekend ensemble signal")
    parser.add_argument("--telegram", action="store_true", help="Send to Telegram")
    parser.add_argument("--dry-run", action="store_true", help="Use last Friday's data")
    args = parser.parse_args()

    result = get_signals(dry_run=args.dry_run)
    message = format_message(result)
    print(message)

    if args.telegram and "error" not in result:
        asyncio.run(send_telegram(message))


if __name__ == "__main__":
    main()
