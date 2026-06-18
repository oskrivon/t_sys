"""Run the icebreaker Bybit collector.

Smoke (60s, first 5 watchlist symbols):
    python scripts/run_icebreaker_collector.py --duration 60 --limit 5

Production (server, foreground / under a supervisor):
    python scripts/run_icebreaker_collector.py --out /root/trading/data/icebreaker
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.icebreaker.collector import BybitCollectorFeed
from src.icebreaker.live_bybit import BybitCollectorClient
from src.icebreaker.recorder import ParquetRecorder
from src.icebreaker.watchlist import load_watchlist

DEFAULT_WATCHLIST = ROOT / "config" / "icebreaker_watchlist.json"
DEFAULT_OUT = ROOT / "data" / "icebreaker"


async def main_async(args) -> int:
    wl = load_watchlist(args.watchlist)
    subs = wl.subscriptions_by_exchange()
    symbols = sorted(subs.get("bybit", set()))
    if args.limit:
        symbols = symbols[:args.limit]
    if not symbols:
        print("[FAIL] no bybit symbols in watchlist")
        return 1

    print(f"  watchlist: {args.watchlist}")
    print(f"  symbols ({len(symbols)}): {', '.join(symbols)}")
    print(f"  out: {args.out}  depth={args.depth}  duration={args.duration or 'inf'}s")

    recorder = ParquetRecorder(args.out, flush_rows=args.flush_rows)
    feed = BybitCollectorFeed(recorder, snap_every_n_deltas=args.snap_every)
    client = BybitCollectorClient(feed, symbols, depth=args.depth)

    await client.run(duration_s=args.duration)

    print("\n  === collector stats ===")
    print(f"  ws frames:      {client.stats['frames']}")
    print(f"  book msgs:      {feed.stats['book_msgs']}")
    print(f"  trade msgs:     {feed.stats['trade_msgs']}")
    print(f"  resyncs:        {feed.stats['resyncs']}")
    print(f"  reconnects:     {client.stats['reconnects']}")
    print(f"  rows written:   {recorder.rows_written}")
    print(f"  parquet files:  {recorder.files_written}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--duration", type=float, default=None, help="seconds (omit = forever)")
    p.add_argument("--limit", type=int, default=None, help="cap number of symbols")
    p.add_argument("--depth", type=int, default=50)
    p.add_argument("--flush-rows", type=int, default=5000)
    p.add_argument("--snap-every", type=int, default=200)
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
