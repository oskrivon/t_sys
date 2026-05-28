#!/usr/bin/env python3
"""V-Bottom dip buying — paper trading runner.

Checks BTC hourly for dip-buy signals. Runs as cron or continuous loop.

Strategy: BTC drop ≥3% in 24h + NATR high + NQ T-1 not down → buy, hold 24h.
Honest backtest: CAGR +25%/yr, Sharpe 1.05, WR 60%.

Usage:
    python scripts/run_v_bottom.py check          # one-shot: check signal + exits
    python scripts/run_v_bottom.py status         # show current state
    python scripts/run_v_bottom.py history        # show trade history

Cron (hourly):
    0 * * * *  cd /root/trading && python3 scripts/run_v_bottom.py check >> data/logs/v_bottom.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

logger = structlog.get_logger()

DB_PATH = "data/v_bottom_paper.db"
STATE_PATH = "data/v_bottom_state.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS v_bottom_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    entry_price REAL NOT NULL,
    exit_price REAL,
    drop_pct REAL NOT NULL,
    natr REAL,
    nq_ret REAL,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
"""


def init_db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def load_state() -> dict:
    """Load rolling state (recent closes, NATR history)."""
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"recent_closes": [], "recent_highs": [], "recent_lows": [],
                "natr_values": []}


def save_state(state: dict):
    Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _bybit(**kwargs) -> "ccxt.bybit":
    """Create Bybit ccxt instance (with optional proxy from env)."""
    import ccxt
    import os
    cfg = {"enableRateLimit": True, **kwargs}
    ex = ccxt.bybit(cfg)
    proxy = os.environ.get("BYBIT_PROXY")
    if proxy:
        ex.proxies = {"http": proxy, "https": proxy}
    return ex


def fetch_btc_price(retries: int = 3, delay: float = 5.0) -> dict | None:
    """Fetch current BTC OHLC from Bybit."""
    import time
    for attempt in range(1, retries + 1):
        try:
            ex = _bybit()
            ticker = ex.fetch_ticker("BTC/USDT:USDT")
            return {
                "price": ticker["last"],
                "high": ticker["high"],
                "low": ticker["low"],
            }
        except Exception as e:
            logger.warning("btc_price_retry", attempt=attempt, retries=retries,
                           error=str(e))
            if attempt < retries:
                time.sleep(delay * attempt)
    logger.error("btc_price_failed", retries=retries)
    return None


def fetch_btc_ohlcv_24h(retries: int = 3, delay: float = 5.0) -> list[dict] | None:
    """Fetch last 24h of BTC 1h candles for accurate drop detection."""
    import time
    for attempt in range(1, retries + 1):
        try:
            ex = _bybit()
            since = int((datetime.now(timezone.utc) - timedelta(hours=26)).timestamp() * 1000)
            ohlcv = ex.fetch_ohlcv("BTC/USDT:USDT", "1h", since=since, limit=30)
            return [{"ts": o[0], "open": o[1], "high": o[2], "low": o[3],
                     "close": o[4], "volume": o[5]} for o in ohlcv]
        except Exception as e:
            logger.warning("btc_ohlcv_retry", attempt=attempt, retries=retries,
                           error=str(e))
            if attempt < retries:
                time.sleep(delay * attempt)
    logger.error("btc_ohlcv_failed", retries=retries)
    return None


def compute_natr(candles: list[dict], period: int = 14) -> float:
    """Compute NATR from candle list."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"] - candles[i-1]["close"]),
        )
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    return atr / candles[-1]["close"] * 100 if candles[-1]["close"] > 0 else 0


def fetch_nq_t1_return() -> float | None:
    """Fetch QQQ T-1 (yesterday) daily return."""
    try:
        import yfinance as yf
        d = yf.download("QQQ", period="5d", interval="1d", progress=False)
        if d.empty or len(d) < 3:
            return None
        d = d.reset_index()
        if isinstance(d.columns, __import__("pandas").MultiIndex):
            d.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in d.columns]
        else:
            d.columns = [c.lower() for c in d.columns]
        # T-1: use second-to-last row's return
        ret = (d["close"].iloc[-2] - d["close"].iloc[-3]) / d["close"].iloc[-3] * 100
        return float(ret)
    except Exception as e:
        logger.warning("nq_fetch_failed", error=str(e))
        return None


# ======================================================================
# Commands
# ======================================================================

def cmd_check(args):
    """One-shot: check for entry signal + check open position exit."""
    conn = init_db()
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    # --- Check open position for exit ---
    open_trade = conn.execute(
        "SELECT * FROM v_bottom_trades WHERE status='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if open_trade:
        entry_time = datetime.fromisoformat(open_trade["entry_time"])
        hours_held = (now - entry_time).total_seconds() / 3600

        btc = fetch_btc_price()
        if btc is None:
            print("ERROR: cannot fetch BTC price")
            conn.close()
            return

        price = btc["price"]

        if hours_held >= 24:
            # Exit
            pnl = (price - open_trade["entry_price"]) / open_trade["entry_price"] * 100
            conn.execute(
                "UPDATE v_bottom_trades SET status='closed', exit_time=?, exit_price=?, pnl_pct=? WHERE id=?",
                (now_str, price, pnl, open_trade["id"]),
            )
            conn.commit()

            logger.info("v_bottom_exit", price=price, pnl=f"{pnl:+.2f}%",
                        hours=f"{hours_held:.1f}")

            stats = _compute_stats(conn)
            print(f"EXIT: ${price:,.0f}  PnL: {pnl:+.2f}%  Hold: {hours_held:.1f}h")
            print(f"  Stats: {stats['n']} trades, WR {stats['wr']:.0f}%, "
                  f"Total: {stats['total_pnl']:+.1f}%")

            # Telegram alert on exit
            try:
                from src.weekend.runner import _telegram_config
                from src.weekend.alerter import send_telegram
                import asyncio
                tg = _telegram_config()
                if tg:
                    msg = (f"V-BOTTOM EXIT (paper)\n"
                           f"BTC ${price:,.0f}\n"
                           f"PnL: {pnl:+.2f}%\n"
                           f"Hold: {hours_held:.1f}h\n"
                           f"Stats: {stats['n']} trades, WR {stats['wr']:.0f}%")
                    asyncio.run(send_telegram(msg, tg[0], tg[1]))
            except Exception:
                pass
        else:
            upnl = (price - open_trade["entry_price"]) / open_trade["entry_price"] * 100
            logger.info("v_bottom_holding", price=price, upnl=f"{upnl:+.2f}%",
                        hours=f"{hours_held:.1f}h", exit_in=f"{24-hours_held:.1f}h")
            print(f"HOLDING: ${price:,.0f}  uPnL: {upnl:+.2f}%  "
                  f"Exit in: {24-hours_held:.1f}h")

        conn.close()
        return

    # --- No open position: check for entry signal ---
    candles = fetch_btc_ohlcv_24h()
    if candles is None or len(candles) < 20:
        print("ERROR: cannot fetch candles")
        conn.close()
        return

    current_price = candles[-1]["close"]

    # Always compute and accumulate NATR (even without drop signal)
    state = load_state()
    natr = compute_natr(candles, 14)
    state["natr_values"].append(natr)
    if len(state["natr_values"]) > 5000:
        state["natr_values"] = state["natr_values"][-5000:]
    save_state(state)

    natr_median = float(__import__("numpy").median(state["natr_values"])) if len(state["natr_values"]) > 50 else 0

    # Drop detection
    window_closes = [c["close"] for c in candles[-24:]]
    window_high = max(window_closes)
    drop_pct = (window_high - current_price) / window_high * 100

    if drop_pct < 3.0:
        logger.info("v_bottom_no_drop", price=current_price,
                    drop=f"{drop_pct:.2f}%")
        print(f"NO SIGNAL: drop {drop_pct:.2f}% (need 3.0%)")
        conn.close()
        return

    # NATR filter (natr + natr_median already computed above)
    if natr_median > 0 and natr <= natr_median:
        logger.info("v_bottom_low_natr", natr=f"{natr:.3f}",
                    median=f"{natr_median:.3f}")
        print(f"SKIP: NATR {natr:.3f} <= median {natr_median:.3f} (low vol)")
        conn.close()
        return

    # NQ T-1 filter
    nq_ret = fetch_nq_t1_return()
    if nq_ret is not None and nq_ret < -0.5:
        logger.info("v_bottom_macro_selloff", nq_ret=f"{nq_ret:+.2f}%")
        print(f"SKIP: NQ T-1 = {nq_ret:+.2f}% (macro selloff)")
        conn.close()
        return

    # --- SIGNAL! Paper entry ---
    conn.execute(
        """INSERT INTO v_bottom_trades
           (entry_time, entry_price, drop_pct, natr, nq_ret, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?)""",
        (now_str, current_price, drop_pct, natr, nq_ret, now_str),
    )
    conn.commit()

    logger.warning("v_bottom_entry", price=current_price,
                   drop=f"{drop_pct:.2f}%", natr=f"{natr:.3f}",
                   nq_ret=f"{nq_ret:+.2f}%" if nq_ret else "N/A")

    print(f"ENTRY: BTC ${current_price:,.0f}")
    print(f"  Drop: {drop_pct:.2f}%  NATR: {natr:.3f}  "
          f"NQ T-1: {nq_ret:+.2f}%" if nq_ret else "N/A")
    print(f"  Exit in 24h ({(now + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M')} UTC)")

    # Send telegram alert
    try:
        from src.weekend.runner import _telegram_config
        from src.weekend.alerter import send_telegram
        import asyncio
        tg = _telegram_config()
        if tg:
            msg = (f"V-BOTTOM ENTRY (paper)\n"
                   f"BTC ${current_price:,.0f}\n"
                   f"Drop: {drop_pct:.2f}%\n"
                   f"NATR: {natr:.3f}\n"
                   f"NQ T-1: {nq_ret:+.2f}%" if nq_ret else "N/A")
            asyncio.run(send_telegram(msg, tg[0], tg[1]))
    except Exception:
        pass

    conn.close()


def cmd_status(args):
    """Show current state."""
    conn = init_db()
    open_trade = conn.execute(
        "SELECT * FROM v_bottom_trades WHERE status='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if open_trade:
        btc = fetch_btc_price()
        price = btc["price"] if btc else 0
        entry_time = datetime.fromisoformat(open_trade["entry_time"])
        hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
        upnl = (price - open_trade["entry_price"]) / open_trade["entry_price"] * 100 if price else 0

        print(f"OPEN POSITION:")
        print(f"  Entry: ${open_trade['entry_price']:,.0f} @ {open_trade['entry_time']}")
        print(f"  Current: ${price:,.0f}  uPnL: {upnl:+.2f}%")
        print(f"  Hours held: {hours:.1f}  Exit in: {max(0, 24-hours):.1f}h")
        print(f"  Drop: {open_trade['drop_pct']:.2f}%  NATR: {open_trade['natr']:.3f}")
    else:
        print("No open position.")

    stats = _compute_stats(conn)
    print(f"\nStats: {stats['n']} trades, WR {stats['wr']:.0f}%, "
          f"Avg: {stats['avg_pnl']:+.2f}%, Total: {stats['total_pnl']:+.1f}%")

    # NATR state
    state = load_state()
    if state["natr_values"]:
        med = float(__import__("numpy").median(state["natr_values"]))
        print(f"NATR median: {med:.3f} ({len(state['natr_values'])} samples)")

    conn.close()


def cmd_history(args):
    """Show trade history."""
    conn = init_db()
    trades = conn.execute(
        "SELECT * FROM v_bottom_trades ORDER BY id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()

    if not trades:
        print("No trades yet.")
        conn.close()
        return

    print(f"{'Date':>20} {'Entry':>10} {'Exit':>10} {'PnL':>8} {'Drop':>6} {'Status':>8}")
    print("-" * 68)
    for t in trades:
        exit_p = f"${t['exit_price']:,.0f}" if t["exit_price"] else "-"
        pnl = f"{t['pnl_pct']:+.2f}%" if t["pnl_pct"] is not None else "-"
        print(f"  {t['entry_time'][:16]:>18} ${t['entry_price']:>9,.0f} {exit_p:>10} "
              f"{pnl:>8} {t['drop_pct']:>5.1f}% {t['status']:>8}")

    stats = _compute_stats(conn)
    print(f"\nTotal: {stats['n']} trades, WR {stats['wr']:.0f}%, "
          f"Avg: {stats['avg_pnl']:+.2f}%, Cum: {stats['total_pnl']:+.1f}%")
    conn.close()


def _compute_stats(conn) -> dict:
    rows = conn.execute(
        "SELECT pnl_pct FROM v_bottom_trades WHERE status='closed'"
    ).fetchall()
    if not rows:
        return {"n": 0, "wr": 0, "avg_pnl": 0, "total_pnl": 0}
    pnls = [r["pnl_pct"] for r in rows]
    return {
        "n": len(pnls),
        "wr": sum(1 for p in pnls if p > 0) / len(pnls) * 100,
        "avg_pnl": sum(pnls) / len(pnls),
        "total_pnl": sum(pnls),
    }


def main():
    parser = argparse.ArgumentParser(description="V-Bottom paper trading")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="Check signal + exits")
    sub.add_parser("status", help="Show current state")

    hist = sub.add_parser("history", help="Trade history")
    hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "history":
        cmd_history(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
