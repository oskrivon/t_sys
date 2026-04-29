#!/usr/bin/env python3
"""Weekend 5-WAY Ensemble Signal — BTC weekend direction from cross-asset predictors.

Predictors (OOS validated): KWEB fri, EWJ fri, XLK week, XLE week, USDJPY week.
Signal: 3/5 majority vote -> LONG/SHORT BTC.
Entry: Friday 21:00 UTC. Exit: Sunday 23:00 UTC. SL: 2%.

Usage:
    python scripts/weekend_signal.py friday              # compute & alert
    python scripts/weekend_signal.py friday --dry-run    # use last Friday
    python scripts/weekend_signal.py friday --historical 2026-04-18
    python scripts/weekend_signal.py friday --no-telegram
    python scripts/weekend_signal.py settle              # Sunday: close trade
    python scripts/weekend_signal.py check-sl            # Sat/Sun: check stop-loss
    python scripts/weekend_signal.py history              # show trade history

Cron (server):
    5,10,15 21 * * 5  weekend_signal.py friday
    0 */4 * * 6,0     weekend_signal.py check-sl
    5 23 * * 0        weekend_signal.py settle
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.weekend.config import DEFAULT_CONFIG
from src.weekend.runner import run_friday_signal, run_sl_check, run_sunday_settlement
from src.weekend.state import get_history, get_stats, init_db


def cmd_friday(args):
    result = asyncio.run(run_friday_signal(
        DEFAULT_CONFIG,
        dry_run=args.dry_run,
        historical=args.historical,
        send_alert=not args.no_telegram,
    ))
    if not result.direction:
        sys.exit(0)  # no trade is not an error


def cmd_settle(args):
    result = asyncio.run(run_sunday_settlement(
        DEFAULT_CONFIG,
        send_alert=not args.no_telegram,
    ))
    if result:
        print(f"\nSettled: {result['pnl']:+.2f}%")


def cmd_check_sl(args):
    result = asyncio.run(run_sl_check(
        DEFAULT_CONFIG,
        send_alert=not args.no_telegram,
    ))
    if result:
        print(f"\nSL HIT: {result['pnl']:+.2f}%")
    else:
        print("OK — no SL hit")


def cmd_history(args):
    conn = init_db(DEFAULT_CONFIG.db_path)
    try:
        trades = get_history(conn, limit=args.limit)
        stats = get_stats(conn)

        if not trades:
            print("No trades yet.")
            return

        print(f"\n{'Date':<12s} {'Dir':<6s} {'Entry':>10s} {'Exit':>10s} "
              f"{'P&L':>8s} {'Status':<8s}")
        print("-" * 60)
        for t in trades:
            entry = f"${t['entry_price']:,.0f}" if t["entry_price"] else "-"
            exit_ = f"${t['exit_price']:,.0f}" if t["exit_price"] else "-"
            pnl = f"{t['pnl_pct']:+.2f}%" if t["pnl_pct"] is not None else "-"
            print(f"{t['signal_date']:<12s} {t['direction']:<6s} {entry:>10s} "
                  f"{exit_:>10s} {pnl:>8s} {t['status']:<8s}")

        if stats.get("total", 0) > 0:
            print(f"\nStats ({stats['total']} closed):")
            print(f"  WR: {stats['win_rate']:.0f}%  "
                  f"Avg: {stats['avg_pnl']:+.2f}%  "
                  f"Total: {stats['total_pnl']:+.1f}%")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Weekend 5-WAY ensemble signal for BTC",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # friday
    p_fri = sub.add_parser("friday", help="Compute Friday signal")
    p_fri.add_argument("--dry-run", action="store_true",
                       help="Use last Friday's data")
    p_fri.add_argument("--historical", type=str, default=None,
                       help="Specific date YYYY-MM-DD")
    p_fri.add_argument("--no-telegram", action="store_true",
                       help="Don't send Telegram alert")
    p_fri.set_defaults(func=cmd_friday)

    # settle
    p_set = sub.add_parser("settle", help="Sunday settlement")
    p_set.add_argument("--no-telegram", action="store_true")
    p_set.set_defaults(func=cmd_settle)

    # check-sl
    p_sl = sub.add_parser("check-sl", help="Check stop-loss")
    p_sl.add_argument("--no-telegram", action="store_true")
    p_sl.set_defaults(func=cmd_check_sl)

    # history
    p_hist = sub.add_parser("history", help="Show trade history")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
