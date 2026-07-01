"""Cascade trigger detection: tick bars vs time bars comparison.

Downloads Binance aggTrades, builds tick bars (N trades = 1 bar),
runs the same cascade detection logic, and compares with 1m time bars.

Usage:
    python scripts/research/cascade_tick_bars.py
"""
import ccxt
import time
import numpy as np
from datetime import datetime, timezone, timedelta

binance = ccxt.binanceusdm({"enableRateLimit": True})
binance.load_markets()

SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
           "SUI/USDT:USDT", "AVAX/USDT:USDT"]

# Tick bar size: N trades per bar
TICK_SIZE = 500
# Lookback
DAYS = 3
HOLD_BARS = 10  # hold after trigger (10 bars)
COOLDOWN = 20

FEE = 0.08  # roundtrip taker


def fetch_agg_trades(symbol: str, since_ms: int, limit_trades: int = 200_000) -> list[dict]:
    """Fetch aggTrades from Binance with pagination via fromId."""
    # Binance aggTrades: GET /fapi/v1/aggTrades
    # Params: symbol, fromId, startTime, limit (max 1000)
    market = binance.market(symbol)
    binance_sym = market["id"]  # e.g. ETHUSDT

    trades = []
    params = {"startTime": since_ms, "limit": 1000}

    while len(trades) < limit_trades:
        try:
            raw = binance.fapiPublicGetAggTrades({
                "symbol": binance_sym, **params
            })
        except Exception as e:
            print(f"  Error fetching trades: {e}")
            break

        if not raw:
            break

        for t in raw:
            trades.append({
                "id": int(t["a"]),
                "ts": int(t["T"]),
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "is_sell": t["m"],  # True = maker is buyer = trade is sell
            })

        # Paginate forward
        last_id = int(raw[-1]["a"])
        params = {"fromId": last_id + 1, "limit": 1000}

        if len(raw) < 1000:
            break

        time.sleep(0.1)

    return trades


def build_tick_bars(trades: list[dict], tick_size: int) -> list[dict]:
    """Build tick bars: every tick_size trades = 1 bar."""
    bars = []
    for i in range(0, len(trades) - tick_size + 1, tick_size):
        chunk = trades[i:i + tick_size]
        prices = [t["price"] for t in chunk]
        qty = sum(t["qty"] for t in chunk)
        sell_qty = sum(t["qty"] for t in chunk if t["is_sell"])
        buy_qty = qty - sell_qty

        bars.append({
            "ts": chunk[0]["ts"],
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": qty,
            "n_trades": tick_size,
            "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
            "duration_s": (chunk[-1]["ts"] - chunk[0]["ts"]) / 1000,
        })
    return bars


def fetch_time_bars(symbol: str, since_ms: int) -> list[dict]:
    """Fetch 1m candles for comparison."""
    all_candles = []
    cursor = since_ms
    for _ in range(15):
        batch = binance.fetch_ohlcv(symbol, "1m", since=cursor, limit=1500)
        if not batch:
            break
        all_candles.extend(batch)
        cursor = batch[-1][0] + 1
        time.sleep(0.2)
        if len(batch) < 1500:
            break

    return [{"ts": c[0], "open": c[1], "high": c[2], "low": c[3],
             "close": c[4], "volume": c[5]} for c in all_candles]


def detect_triggers(bars: list[dict], hold_n: int) -> list[dict]:
    """Detect cascade triggers: |ret| > 0.5% + vol > 3x avg."""
    if len(bars) < 30:
        return []

    # Compute returns
    for i in range(1, len(bars)):
        bars[i]["ret"] = (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100

    avg_vol = np.mean([b["volume"] for b in bars[20:]])

    trades = []
    cooldown = 0

    for i in range(20, len(bars) - hold_n - 5):
        if cooldown > 0:
            cooldown -= 1
            continue

        b = bars[i]
        if "ret" not in b:
            continue

        if abs(b["ret"]) > 0.5 and b["volume"] > avg_vol * 3:
            direction = 1 if b["ret"] > 0 else -1
            entry = b["close"]

            # Fixed hold
            results = {}
            for hold in [3, 5, hold_n]:
                idx = min(i + hold, len(bars) - 1)
                exit_p = bars[idx]["close"]
                pnl = direction * (exit_p - entry) / entry * 100
                results[f"hold_{hold}"] = pnl

            # MFE
            mfe = 0
            for j in range(i + 1, min(i + hold_n + 5, len(bars))):
                if direction > 0:
                    m = (bars[j]["high"] - entry) / entry * 100
                else:
                    m = (entry - bars[j]["low"]) / entry * 100
                mfe = max(mfe, m)
            results["mfe"] = mfe

            # Trailing stop: 0.3% from peak
            best = 0
            trail_exit = 0
            for j in range(i + 1, min(i + hold_n + 5, len(bars))):
                if direction > 0:
                    move = (bars[j]["high"] - entry) / entry * 100
                    drawback = (bars[j]["close"] - entry) / entry * 100
                else:
                    move = (entry - bars[j]["low"]) / entry * 100
                    drawback = (entry - bars[j]["close"]) / entry * 100
                if move > best:
                    best = move
                if best > 0.3 and best - drawback > 0.3:
                    trail_exit = drawback
                    break
            if trail_exit == 0:
                trail_exit = results.get(f"hold_{hold_n}", 0)
            results["trailing"] = trail_exit

            dt = datetime.fromtimestamp(b["ts"] / 1000, tz=timezone.utc)

            extra = {}
            if "sell_pct" in b:
                extra["sell_pct"] = b["sell_pct"]
                extra["duration_s"] = b["duration_s"]

            trades.append({
                "date": dt.strftime("%m-%d %H:%M:%S"),
                "trigger": b["ret"],
                "vol_x": b["volume"] / avg_vol,
                **results,
                **extra,
            })
            cooldown = COOLDOWN

    return trades


def print_stats(trades: list[dict], label: str):
    """Print summary stats for a set of trades."""
    if not trades:
        print(f"  {label}: no triggers found\n")
        return

    pnls_trail = np.array([t["trailing"] for t in trades])
    mfes = np.array([t["mfe"] for t in trades])
    net = pnls_trail - FEE

    # Best hold period
    hold_keys = [k for k in trades[0] if k.startswith("hold_")]
    best_hold = None
    best_avg = -999
    for hk in hold_keys:
        vals = np.array([t[hk] for t in trades])
        if vals.mean() > best_avg:
            best_avg = vals.mean()
            best_hold = hk

    print(f"  {label}")
    print(f"    Triggers:     {len(trades)}")
    print(f"    WR (trail):   {(pnls_trail > 0).mean() * 100:.0f}%")
    print(f"    Avg trail:    {pnls_trail.mean():+.3f}%  net: {net.mean():+.3f}%")
    print(f"    Avg MFE:      {mfes.mean():+.3f}%")
    print(f"    Best hold:    {best_hold} = {best_avg:+.3f}%")
    if pnls_trail.std() > 0:
        print(f"    Sharpe:       {net.mean() / pnls_trail.std():.2f}")

    if "sell_pct" in trades[0]:
        sell_pcts = [t["sell_pct"] for t in trades]
        durations = [t["duration_s"] for t in trades]
        print(f"    Avg sell%:    {np.mean(sell_pcts):.1f}%  (>50 = sell pressure)")
        print(f"    Avg bar dur:  {np.mean(durations):.1f}s  (shorter = hotter)")

    print()


def main():
    print("=" * 70)
    print("  CASCADE TRIGGER: TICK BARS vs TIME BARS")
    print("=" * 70)
    print(f"\n  Tick bar size: {TICK_SIZE} trades/bar")
    print(f"  Lookback: {DAYS} days")
    print(f"  Fee: {FEE}% roundtrip\n")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp() * 1000)

    all_tick_trades = []
    all_time_trades = []

    for sym in SYMBOLS:
        coin = sym.split("/")[0]
        print(f"\n--- {coin} ---")

        # Fetch aggTrades
        print(f"  Fetching aggTrades...", end=" ", flush=True)
        raw_trades = fetch_agg_trades(sym, since_ms)
        print(f"{len(raw_trades):,} trades")

        if len(raw_trades) < TICK_SIZE * 30:
            print(f"  Not enough trades, skipping")
            continue

        # Build tick bars
        tick_bars = build_tick_bars(raw_trades, TICK_SIZE)
        print(f"  Tick bars: {len(tick_bars)} (avg duration: "
              f"{np.mean([b['duration_s'] for b in tick_bars]):.0f}s)")

        # Fetch time bars
        print(f"  Fetching 1m candles...", end=" ", flush=True)
        time_bars = fetch_time_bars(sym, since_ms)
        print(f"{len(time_bars)} candles")

        # Detect on both
        tick_triggers = detect_triggers(tick_bars, hold_n=10)
        time_triggers = detect_triggers(time_bars, hold_n=10)

        for t in tick_triggers:
            t["sym"] = coin
        for t in time_triggers:
            t["sym"] = coin

        all_tick_trades.extend(tick_triggers)
        all_time_trades.extend(time_triggers)

        print_stats(time_triggers, f"{coin} TIME BARS (1m)")
        print_stats(tick_triggers, f"{coin} TICK BARS ({TICK_SIZE})")

    # Overall comparison
    print("\n" + "=" * 70)
    print("  OVERALL COMPARISON")
    print("=" * 70)

    print_stats(all_time_trades, f"ALL COINS — TIME BARS (1m)")
    print_stats(all_tick_trades, f"ALL COINS — TICK BARS ({TICK_SIZE})")

    # Delta
    if all_tick_trades and all_time_trades:
        tick_avg = np.mean([t["trailing"] for t in all_tick_trades]) - FEE
        time_avg = np.mean([t["trailing"] for t in all_time_trades]) - FEE
        tick_wr = (np.array([t["trailing"] for t in all_tick_trades]) > 0).mean() * 100
        time_wr = (np.array([t["trailing"] for t in all_time_trades]) > 0).mean() * 100

        print("  DELTA (tick - time):")
        print(f"    Triggers:  {len(all_tick_trades)} vs {len(all_time_trades)} "
              f"({len(all_tick_trades) - len(all_time_trades):+d})")
        print(f"    Net avg:   {tick_avg:+.3f}% vs {time_avg:+.3f}% "
              f"(diff: {tick_avg - time_avg:+.3f}%)")
        print(f"    Win rate:  {tick_wr:.0f}% vs {time_wr:.0f}%")

    # Show individual tick bar trades (top by MFE)
    if all_tick_trades:
        print(f"\n\n--- TOP TICK BAR TRIGGERS (by MFE) ---\n")
        print(f"  {'Coin':>5s} {'Date':>14s} {'Trig':>7s} {'VolX':>5s} "
              f"{'Trail':>7s} {'MFE':>7s} {'Sell%':>6s} {'BarSec':>7s}")
        print("  " + "-" * 62)
        for t in sorted(all_tick_trades, key=lambda x: x["mfe"], reverse=True)[:20]:
            sell_pct = f"{t.get('sell_pct', 0):.0f}%" if "sell_pct" in t else "?"
            dur = f"{t.get('duration_s', 0):.0f}s" if "duration_s" in t else "?"
            print(f"  {t['sym']:>5s} {t['date']:>14s} {t['trigger']:>+6.1f}% "
                  f"{t['vol_x']:>4.0f}x {t['trailing']:>+6.2f}% "
                  f"{t['mfe']:>+6.2f}% {sell_pct:>6s} {dur:>7s}")


if __name__ == "__main__":
    main()
