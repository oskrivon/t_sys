#!/usr/bin/env python3
"""Volume Ranking L/S — daily paper trading bot.

Runs once daily: computes volume ratios, determines long/short split,
records positions and tracks P&L.

Usage:
    python scripts/run_volume_ranking.py              # run once (daily rebalance)
    python scripts/run_volume_ranking.py --status      # show current positions + P&L
    python scripts/run_volume_ranking.py --loop         # run in loop (daily at 00:05 UTC)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import structlog
import ccxt.async_support as ccxt

log = structlog.get_logger()

DB_PATH = Path("data/volume_ranking_paper.db")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT", "TRX/USDT", "LINK/USDT", "ADA/USDT",
    "AVAX/USDT", "NEAR/USDT", "LTC/USDT", "FET/USDT", "UNI/USDT",
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "ATOM/USDT", "ETC/USDT", "APT/USDT", "SEI/USDT",
    "JUP/USDT", "WLD/USDT", "PENDLE/USDT", "ENA/USDT", "TAO/USDT",
    "GALA/USDT", "MANTA/USDT", "STRK/USDT",
]

SHORT_WINDOW = 7
LONG_WINDOW = 30
TOP_PCT = 0.5
FEE_PER_SIDE = 0.0004  # taker 4 bps

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    positions_json TEXT NOT NULL,
    n_longs INTEGER,
    n_shorts INTEGER,
    turnover_pct REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    gross_pnl_pct REAL,
    fee_pct REAL,
    net_pnl_pct REAL,
    long_pnl_pct REAL,
    short_pnl_pct REAL,
    cumulative_pnl_pct REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_date ON daily_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_pnl_date ON daily_pnl(date);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


async def fetch_volume_ratios(exchange) -> dict[str, float]:
    """Fetch 7d/30d volume ratios for all symbols."""
    ratios = {}
    for symbol in SYMBOLS:
        try:
            candles = await exchange.fetch_ohlcv(symbol, "1d", limit=LONG_WINDOW + 5)
            if not candles or len(candles) < LONG_WINDOW:
                continue
            volumes = [c[5] for c in candles]
            vol_short = np.mean(volumes[-SHORT_WINDOW:])
            vol_long = np.mean(volumes[-LONG_WINDOW:])
            if vol_long > 0:
                ratios[symbol] = vol_short / vol_long
        except Exception as e:
            log.debug("fetch_error", symbol=symbol, error=str(e))
    return ratios


async def fetch_current_prices(exchange, symbols: list[str]) -> dict[str, float]:
    """Fetch current prices for symbols."""
    prices = {}
    for symbol in symbols:
        try:
            ticker = await exchange.fetch_ticker(symbol)
            prices[symbol] = ticker["last"]
        except Exception:
            pass
    return prices


def compute_positions(ratios: dict[str, float]) -> dict[str, str]:
    """Rank by volume ratio, assign long/short."""
    sorted_syms = sorted(ratios.keys(), key=lambda s: ratios[s], reverse=True)
    n = len(sorted_syms)
    n_long = int(n * TOP_PCT)

    positions = {}
    for i, sym in enumerate(sorted_syms):
        positions[sym] = "long" if i < n_long else "short"
    return positions


async def run_rebalance():
    """Run daily rebalance."""
    conn = init_db()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # Check if already ran today
    existing = conn.execute("SELECT id FROM daily_snapshots WHERE date = ?", (today,)).fetchone()
    if existing:
        print(f"Already rebalanced today ({today}). Use --status to check.")
        conn.close()
        return

    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        print(f"Fetching volume ratios for {len(SYMBOLS)} symbols...")
        ratios = await fetch_volume_ratios(exchange)
        print(f"Got ratios for {len(ratios)} symbols")

        if len(ratios) < 10:
            print("Not enough data!")
            return

        # Compute new positions
        new_positions = compute_positions(ratios)
        n_longs = sum(1 for v in new_positions.values() if v == "long")
        n_shorts = len(new_positions) - n_longs

        # Get previous positions for turnover calc
        prev = conn.execute(
            "SELECT positions_json FROM daily_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        prev_positions = json.loads(prev["positions_json"]) if prev else {}

        # Calculate turnover
        changed = sum(1 for s in new_positions
                      if s not in prev_positions or prev_positions[s] != new_positions[s])
        turnover = changed / len(new_positions) if new_positions else 0

        # Calculate P&L from previous positions
        if prev_positions:
            prices = await fetch_current_prices(exchange, list(prev_positions.keys()))

            # We need yesterday's prices - approximate from last snapshot
            prev_snapshot_date = conn.execute(
                "SELECT date FROM daily_snapshots ORDER BY date DESC LIMIT 1"
            ).fetchone()

            if prev_snapshot_date:
                # Fetch yesterday's close prices via 1d candle
                long_rets, short_rets = [], []
                for sym, side in prev_positions.items():
                    try:
                        candles = await exchange.fetch_ohlcv(sym, "1d", limit=2)
                        if candles and len(candles) >= 2:
                            prev_close = candles[-2][4]
                            curr_close = candles[-1][4]
                            ret = (curr_close - prev_close) / prev_close
                            if side == "long":
                                long_rets.append(ret)
                            else:
                                short_rets.append(-ret)
                    except Exception:
                        pass

                if long_rets or short_rets:
                    long_pnl = np.mean(long_rets) if long_rets else 0
                    short_pnl = np.mean(short_rets) if short_rets else 0
                    gross = (long_pnl + short_pnl) / 2
                    fee = turnover * FEE_PER_SIDE * 2
                    net = gross - fee

                    # Get cumulative
                    last_cum = conn.execute(
                        "SELECT cumulative_pnl_pct FROM daily_pnl ORDER BY date DESC LIMIT 1"
                    ).fetchone()
                    cum = (last_cum["cumulative_pnl_pct"] if last_cum else 0) + net * 100

                    conn.execute(
                        "INSERT INTO daily_pnl (date, gross_pnl_pct, fee_pct, net_pnl_pct, "
                        "long_pnl_pct, short_pnl_pct, cumulative_pnl_pct, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (today, gross*100, fee*100, net*100, long_pnl*100, short_pnl*100,
                         cum, now.isoformat()),
                    )
                    print(f"P&L: gross {gross*100:+.3f}%, net {net*100:+.3f}%, cumulative {cum:+.2f}%")

        # Save new positions
        conn.execute(
            "INSERT INTO daily_snapshots (date, positions_json, n_longs, n_shorts, "
            "turnover_pct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (today, json.dumps(new_positions), n_longs, n_shorts,
             turnover * 100, now.isoformat()),
        )
        conn.commit()

        print(f"\nRebalanced: {n_longs} longs, {n_shorts} shorts, turnover {turnover*100:.1f}%")

        # Show top/bottom
        sorted_syms = sorted(ratios.keys(), key=lambda s: ratios[s], reverse=True)
        print(f"\nTop 5 (long):  {', '.join(f'{s} ({ratios[s]:.2f})' for s in sorted_syms[:5])}")
        print(f"Bottom 5 (short): {', '.join(f'{s} ({ratios[s]:.2f})' for s in sorted_syms[-5:])}")

    finally:
        await exchange.close()
        conn.close()


def show_status():
    """Show current positions and P&L history."""
    conn = init_db()

    # Current positions
    snap = conn.execute(
        "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()

    if snap:
        positions = json.loads(snap["positions_json"])
        longs = [s for s, v in positions.items() if v == "long"]
        shorts = [s for s, v in positions.items() if v == "short"]
        print(f"=== Volume Ranking Paper Trading ===")
        print(f"Date: {snap['date']}")
        print(f"Longs ({len(longs)}): {', '.join(longs[:10])}{'...' if len(longs) > 10 else ''}")
        print(f"Shorts ({len(shorts)}): {', '.join(shorts[:10])}{'...' if len(shorts) > 10 else ''}")
        print(f"Turnover: {snap['turnover_pct']:.1f}%")
    else:
        print("No snapshots yet. Run without --status first.")

    # P&L history
    pnl_rows = conn.execute("SELECT * FROM daily_pnl ORDER BY date").fetchall()
    if pnl_rows:
        print(f"\n=== P&L History ===")
        print(f"{'Date':>12} {'Gross':>8} {'Fee':>7} {'Net':>8} {'Cum':>8}")
        for r in pnl_rows:
            print(f"{r['date']:>12} {r['gross_pnl_pct']:>+7.3f}% {r['fee_pct']:>6.3f}% "
                  f"{r['net_pnl_pct']:>+7.3f}% {r['cumulative_pnl_pct']:>+7.2f}%")
        print(f"\nTotal days: {len(pnl_rows)}")
        total_net = sum(r["net_pnl_pct"] for r in pnl_rows)
        print(f"Total net P&L: {total_net:+.2f}%")
        win_days = sum(1 for r in pnl_rows if r["net_pnl_pct"] > 0)
        print(f"Win rate: {win_days}/{len(pnl_rows)} ({win_days/len(pnl_rows)*100:.0f}%)")

    conn.close()


async def run_loop():
    """Run daily at 00:05 UTC."""
    print("Volume Ranking loop started. Rebalance daily at 00:05 UTC.")
    while True:
        now = datetime.now(timezone.utc)
        # Next 00:05 UTC
        target = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"Next rebalance at {target.isoformat()}, waiting {wait/3600:.1f}h")
        await asyncio.sleep(wait)
        try:
            await run_rebalance()
        except Exception as e:
            log.error("rebalance_error", error=str(e))
            await asyncio.sleep(60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.loop:
        asyncio.run(run_loop())
    else:
        asyncio.run(run_rebalance())


if __name__ == "__main__":
    main()
