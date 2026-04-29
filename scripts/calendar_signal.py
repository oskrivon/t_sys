#!/usr/bin/env python3
"""Calendar-based BTC signals: pre-FOMC drift + post-quarterly-expiry dump.

Pre-FOMC LONG: BTC rises 8h before FOMC decision (WR 70%, 8/yr)
Post-Q-Expiry SHORT: BTC drops 24h after quarterly options expiry (WR 58%, 4/yr)
Combined: Sharpe_net 1.06, ~14% annual, 20 trades/yr.

Usage:
    python3 scripts/calendar_signal.py entry              # enter if event now
    python3 scripts/calendar_signal.py exit               # exit if hold expired
    python3 scripts/calendar_signal.py check-sl           # check stop-loss
    python3 scripts/calendar_signal.py next [--limit N]   # upcoming events
    python3 scripts/calendar_signal.py history [--limit N] # trade history
    python3 scripts/calendar_signal.py entry --dry-run    # simulate entry for today

Cron:
    0,5 10 * * 3   calendar_signal.py entry     # FOMC entry (Wed 10:00 UTC)
    0 18 * * 3     calendar_signal.py exit      # FOMC exit (Wed 18:00 UTC)
    0,5 8 * * 5    calendar_signal.py entry     # Q-expiry entry (Fri 08:00 UTC)
    0 */4 * * *    calendar_signal.py exit      # exit check every 4h
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.calendar_signal.config import DEFAULT_CONFIG
from src.calendar_signal.events import next_events
from src.calendar_signal.runner import run_entry, run_exit, run_sl_check
from src.calendar_signal.state import get_history, get_stats, init_db


def cmd_entry(args):
    now = None
    if args.dry_run:
        # Use current time but extend tolerance to find closest event
        now = datetime.now(timezone.utc)
    result = asyncio.run(run_entry(
        DEFAULT_CONFIG,
        now=now,
        send_alert=not args.no_telegram,
    ))
    if result:
        print(f"\nEntered: {result.event_type.value} {result.direction.upper()}")


def cmd_exit(args):
    results = asyncio.run(run_exit(
        DEFAULT_CONFIG,
        send_alert=not args.no_telegram,
    ))
    if results:
        for r in results:
            print(f"\nExited: {r['event_type']} P&L={r['pnl']:+.2f}%")
    else:
        print("No trades to exit")


def cmd_check_sl(args):
    results = asyncio.run(run_sl_check(
        DEFAULT_CONFIG,
        send_alert=not args.no_telegram,
    ))
    if results:
        for r in results:
            print(f"\nSL HIT: {r['event_type']} P&L={r['pnl']:+.2f}%")
    else:
        print("OK - no SL hit (or SL disabled)")


def cmd_next(args):
    now = datetime.now(timezone.utc)
    upcoming = next_events(DEFAULT_CONFIG, now, n=args.limit)
    if not upcoming:
        print("No upcoming events")
        return
    print(f"\n{'Date':<12s} {'Type':<25s} {'Dir':<6s} {'Entry':>6s} {'Hold':>5s}")
    print("-" * 58)
    for e in upcoming:
        print(f"{e.date:<12s} {e.event_type.value:<25s} {e.direction:<6s} "
              f"{e.entry_hour_utc:>2d}:00  {e.hold_hours:>3d}h")


def cmd_history(args):
    conn = init_db(DEFAULT_CONFIG.db_path)
    try:
        trades = get_history(conn, limit=args.limit)
        stats = get_stats(conn)

        if not trades:
            print("No trades yet.")
            return

        print(f"\n{'Date':<12s} {'Type':<22s} {'Dir':<6s} {'Entry':>10s} "
              f"{'Exit':>10s} {'P&L':>8s} {'Status':<8s}")
        print("-" * 80)
        for t in trades:
            entry = f"${t['entry_price']:,.0f}" if t["entry_price"] else "-"
            exit_ = f"${t['exit_price']:,.0f}" if t["exit_price"] else "-"
            pnl = f"{t['pnl_pct']:+.2f}%" if t["pnl_pct"] is not None else "-"
            etype = t["event_type"][:20]
            print(f"{t['event_date']:<12s} {etype:<22s} {t['direction']:<6s} "
                  f"{entry:>10s} {exit_:>10s} {pnl:>8s} {t['status']:<8s}")

        if stats.get("total", 0) > 0:
            print(f"\nStats ({stats['total']} closed):")
            print(f"  WR: {stats['win_rate']:.0f}%  "
                  f"Avg: {stats['avg_pnl']:+.2f}%  "
                  f"Total: {stats['total_pnl']:+.1f}%")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Calendar-based BTC signals (FOMC + Q-expiry)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_entry = sub.add_parser("entry", help="Enter if event now")
    p_entry.add_argument("--dry-run", action="store_true")
    p_entry.add_argument("--no-telegram", action="store_true")
    p_entry.set_defaults(func=cmd_entry)

    p_exit = sub.add_parser("exit", help="Exit if hold expired")
    p_exit.add_argument("--no-telegram", action="store_true")
    p_exit.set_defaults(func=cmd_exit)

    p_sl = sub.add_parser("check-sl", help="Check stop-loss")
    p_sl.add_argument("--no-telegram", action="store_true")
    p_sl.set_defaults(func=cmd_check_sl)

    p_next = sub.add_parser("next", help="Upcoming events")
    p_next.add_argument("--limit", type=int, default=10)
    p_next.set_defaults(func=cmd_next)

    p_hist = sub.add_parser("history", help="Trade history")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
