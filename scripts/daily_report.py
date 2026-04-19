#!/usr/bin/env python3
"""Daily portfolio report — Telegram + console.

Aggregates stats from both strategies and sends to Telegram.

Usage:
    python scripts/daily_report.py            # print to console
    python scripts/daily_report.py --telegram  # send to Telegram
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paper_trading import db as paper_db
from src.paper_trading.stats import compute_stats

PAPER_DB = Path("data/paper_trades.db")
VR_DB = Path("data/volume_ranking_paper.db")


def get_miro_report() -> str:
    """Generate Miro strategy report."""
    conn = paper_db.get_connection(PAPER_DB)
    stats = compute_stats(conn)

    open_trades = paper_db.get_open_trades(conn)
    recent = paper_db.get_closed_trades(conn)[:5]
    conn.close()

    lines = [
        "--- MIRO S/R + ML + Vision ---",
        f"Trades: {stats.total} ({stats.open} open, {stats.closed} closed)",
        f"Win rate: {stats.win_rate:.1f}% ({stats.wins}W / {stats.losses}L)",
        f"Profit factor: {stats.profit_factor:.2f}",
        f"Total P&L: {stats.total_pnl_pct:+.2f}%",
        f"Expectancy: {stats.expectancy_pct:+.2f}%/trade",
    ]

    if open_trades:
        lines.append(f"\nOpen ({len(open_trades)}):")
        for t in open_trades[:5]:
            ml = f"ML:{t['ml_score']:.0%}" if t["ml_score"] else ""
            vs = f"V:{t['vision_score']}" if t["vision_score"] else ""
            lines.append(
                f"  {t['symbol']} {t['direction'].upper()} "
                f"@ {t['entry_price']:.8g} {ml} {vs}"
            )

    if recent:
        lines.append(f"\nRecent closed:")
        for t in recent:
            icon = "W" if t["pnl_pct"] and t["pnl_pct"] > 0 else "L"
            pnl = f"{t['pnl_pct']:+.2f}%" if t["pnl_pct"] is not None else "?"
            lines.append(f"  [{icon}] {t['symbol']} {pnl}")

    return "\n".join(lines)


def get_volume_ranking_report() -> str:
    """Generate Volume Ranking report."""
    if not VR_DB.exists():
        return "--- VOLUME RANKING ---\nNot started yet."

    conn = sqlite3.connect(str(VR_DB))
    conn.row_factory = sqlite3.Row

    # Current positions
    snap = conn.execute(
        "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()

    # P&L history
    pnl_rows = conn.execute("SELECT * FROM daily_pnl ORDER BY date DESC").fetchall()

    lines = ["--- VOLUME RANKING L/S ---"]

    if snap:
        positions = json.loads(snap["positions_json"])
        n_longs = sum(1 for v in positions.values() if v == "long")
        n_shorts = len(positions) - n_longs
        lines.append(f"Date: {snap['date']}")
        lines.append(f"Positions: {n_longs} longs, {n_shorts} shorts")
        lines.append(f"Turnover: {snap['turnover_pct']:.1f}%")

    if pnl_rows:
        total_net = sum(r["net_pnl_pct"] for r in pnl_rows)
        n_days = len(pnl_rows)
        win_days = sum(1 for r in pnl_rows if r["net_pnl_pct"] > 0)
        avg_daily = total_net / n_days if n_days > 0 else 0

        lines.append(f"\nP&L ({n_days} days):")
        lines.append(f"Total: {total_net:+.2f}%")
        lines.append(f"Win days: {win_days}/{n_days} ({win_days/n_days*100:.0f}%)")
        lines.append(f"Avg daily: {avg_daily:+.3f}%")

        # Last 5 days
        lines.append(f"\nLast 5 days:")
        for r in pnl_rows[:5]:
            lines.append(f"  {r['date']}: {r['net_pnl_pct']:+.3f}% (cum: {r['cumulative_pnl_pct']:+.2f}%)")
    else:
        lines.append("No P&L data yet (need 2+ days)")

    conn.close()
    return "\n".join(lines)


def get_full_report() -> str:
    """Generate full portfolio report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"=== Portfolio Report {now} ==="

    miro = get_miro_report()
    vr = get_volume_ranking_report()

    return f"{header}\n\n{miro}\n\n{vr}"


async def send_telegram(text: str):
    """Send report to Telegram."""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return

    from telegram import Bot
    bot = Bot(token=token)
    # Split if too long
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=chat_id, text=text[i:i+4000])
    else:
        await bot.send_message(chat_id=chat_id, text=text)
    print("Report sent to Telegram")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    report = get_full_report()
    print(report)

    if args.telegram:
        asyncio.run(send_telegram(report))


if __name__ == "__main__":
    main()
