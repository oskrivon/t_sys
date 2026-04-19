#!/usr/bin/env python3
"""Paper trading CLI — check trades, show stats.

Usage:
    python scripts/paper_trading.py status          # open trades + stats
    python scripts/paper_trading.py check           # check prices, resolve TP/SL
    python scripts/paper_trading.py stats           # detailed stats
    python scripts/paper_trading.py history [N]     # last N closed trades
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ccxt.async_support as ccxt

from src.paper_trading import db
from src.paper_trading.tracker import PaperTrader
from src.paper_trading.stats import compute_stats, format_open_trades, format_recent_trades


async def cmd_check():
    """Fetch current prices, resolve open trades."""
    trader = PaperTrader()
    open_trades = db.get_open_trades(trader.conn)

    if not open_trades:
        print("No open trades to check.")
        return

    symbols = list({t["symbol"] for t in open_trades})
    print(f"Checking {len(open_trades)} open trades across {len(symbols)} symbols...")

    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        resolved = await trader.check_open_trades(exchange)
    finally:
        await exchange.close()
        trader.close()

    if resolved:
        print(f"\nResolved {len(resolved)} trades:")
        for r in resolved:
            icon = "WIN" if r["pnl_pct"] > 0 else "LOSS"
            print(f"  [{icon}] {r['symbol']} {r['direction'].upper()} "
                  f"@ {r['entry']:.8g} -> {r['close_price']:.8g}  "
                  f"{r['pnl_pct']:+.2f}%")
    else:
        print("No trades resolved (all still open).")

    # Show updated stats
    conn = db.get_connection()
    stats = compute_stats(conn)
    print(f"\n{stats.format()}")
    conn.close()


def cmd_status():
    """Show open trades + summary stats."""
    conn = db.get_connection()
    print(format_open_trades(conn))
    print()
    stats = compute_stats(conn)
    print(stats.format())
    conn.close()


def cmd_stats():
    """Detailed statistics."""
    conn = db.get_connection()
    stats = compute_stats(conn)
    print(stats.format())
    conn.close()


def cmd_history(limit: int = 10):
    """Show recent closed trades."""
    conn = db.get_connection()
    print(format_recent_trades(conn, limit))
    print()
    stats = compute_stats(conn)
    print(stats.format())
    conn.close()


def main():
    if len(sys.argv) < 2:
        cmd_status()
        return

    cmd = sys.argv[1]

    if cmd == "check":
        asyncio.run(cmd_check())
    elif cmd == "status":
        cmd_status()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "history":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        cmd_history(limit)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
