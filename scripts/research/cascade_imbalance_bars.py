"""Cascade trigger detection: tick imbalance bars (TIB) experiment.

Tick imbalance bars close when cumulative buy/sell imbalance exceeds
a dynamic threshold. Bars form FASTER during one-sided pressure
(cascades, liquidations) and SLOWER in balanced flow.

Comparison: time bars (1m) vs tick bars (500) vs imbalance bars.

Reference: Lopez de Prado, "Advances in Financial Machine Learning", Ch. 2
"""
import ccxt
import time
import numpy as np
from datetime import datetime, timezone, timedelta

binance = ccxt.binanceusdm({"enableRateLimit": True})
binance.load_markets()

SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
           "SUI/USDT:USDT", "AVAX/USDT:USDT"]

TICK_SIZE = 500          # for regular tick bars
IMBALANCE_INIT = 300     # initial expected bar size for TIB
IMBALANCE_ALPHA = 0.005  # EWMA decay for threshold adaptation
DAYS = 3
HOLD_BARS = 10
COOLDOWN = 20
FEE = 0.08


# ======================================================================
# Data fetching (reused from cascade_tick_bars.py)
# ======================================================================

def fetch_agg_trades(symbol: str, since_ms: int, limit_trades: int = 200_000) -> list[dict]:
    """Fetch aggTrades from Binance with pagination."""
    market = binance.market(symbol)
    binance_sym = market["id"]
    trades = []
    params = {"startTime": since_ms, "limit": 1000}

    while len(trades) < limit_trades:
        try:
            raw = binance.fapiPublicGetAggTrades({"symbol": binance_sym, **params})
        except Exception as e:
            print(f"  Error: {e}")
            break
        if not raw:
            break
        for t in raw:
            trades.append({
                "id": int(t["a"]),
                "ts": int(t["T"]),
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "is_sell": t["m"],  # True = maker is buyer → taker sells
            })
        last_id = int(raw[-1]["a"])
        params = {"fromId": last_id + 1, "limit": 1000}
        if len(raw) < 1000:
            break
        time.sleep(0.1)
    return trades


# ======================================================================
# Bar builders
# ======================================================================

def build_tick_bars(trades: list[dict], tick_size: int) -> list[dict]:
    """Standard tick bars: every N trades = 1 bar."""
    bars = []
    for i in range(0, len(trades) - tick_size + 1, tick_size):
        chunk = trades[i:i + tick_size]
        bars.append(_make_bar(chunk))
    return bars


def build_imbalance_bars(trades: list[dict], init_size: int = 300,
                         alpha: float = 0.005) -> list[dict]:
    """Tick imbalance bars (TIB).

    Bar closes when |cumulative imbalance| exceeds a dynamic threshold.
    Threshold = EWMA of (expected_bar_size * expected_imbalance_per_tick).
    """
    bars = []
    # Running estimates (EWMA)
    expected_T = float(init_size)       # expected bar length (# trades)
    expected_bt = 0.0                   # expected signed tick (buy=+1, sell=-1)

    chunk = []
    theta = 0.0  # cumulative imbalance

    for trade in trades:
        # Sign: +1 buy, -1 sell
        bt = -1.0 if trade["is_sell"] else 1.0
        theta += bt
        chunk.append(trade)

        # Dynamic threshold
        threshold = max(expected_T * abs(expected_bt), 50)

        if abs(theta) >= threshold and len(chunk) >= 20:
            bar = _make_bar(chunk)
            bar["imbalance"] = theta
            bar["threshold"] = threshold
            bars.append(bar)

            # Update EWMA estimates
            T = len(chunk)
            bar_bt = theta / T  # avg signed tick in this bar
            expected_T = alpha * T + (1 - alpha) * expected_T
            expected_bt = alpha * bar_bt + (1 - alpha) * expected_bt

            # Reset
            chunk = []
            theta = 0.0

    return bars


def _make_bar(chunk: list[dict]) -> dict:
    """Create OHLCV bar from trade chunk."""
    prices = [t["price"] for t in chunk]
    qty = sum(t["qty"] for t in chunk)
    sell_qty = sum(t["qty"] for t in chunk if t["is_sell"])
    buy_qty = qty - sell_qty
    n = len(chunk)

    return {
        "ts": chunk[0]["ts"],
        "open": prices[0],
        "high": max(prices),
        "low": min(prices),
        "close": prices[-1],
        "volume": qty,
        "n_trades": n,
        "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
        "buy_pct": buy_qty / qty * 100 if qty > 0 else 50,
        "duration_s": (chunk[-1]["ts"] - chunk[0]["ts"]) / 1000,
    }


def fetch_time_bars(symbol: str, since_ms: int) -> list[dict]:
    """Fetch 1m candles."""
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


# ======================================================================
# Trigger detection
# ======================================================================

def detect_triggers(bars: list[dict], hold_n: int) -> list[dict]:
    """Detect cascade triggers: |ret| > 0.5% + vol > 3x avg."""
    if len(bars) < 30:
        return []

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

            # Trailing stop
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
            for key in ("sell_pct", "buy_pct", "duration_s", "n_trades",
                        "imbalance", "threshold"):
                if key in b:
                    extra[key] = b[key]

            trades.append({
                "date": dt.strftime("%m-%d %H:%M:%S"),
                "trigger": b["ret"],
                "vol_x": b["volume"] / avg_vol,
                **results, **extra,
            })
            cooldown = COOLDOWN
    return trades


def print_stats(trades: list[dict], label: str):
    if not trades:
        print(f"  {label}: no triggers\n")
        return

    pnls = np.array([t["trailing"] for t in trades])
    mfes = np.array([t["mfe"] for t in trades])
    net = pnls - FEE

    hold_keys = [k for k in trades[0] if k.startswith("hold_")]
    best_hold = max(hold_keys, key=lambda k: np.mean([t[k] for t in trades]))
    best_avg = np.mean([t[best_hold] for t in trades])

    print(f"  {label}")
    print(f"    Triggers:     {len(trades)}")
    print(f"    WR (trail):   {(pnls > 0).mean() * 100:.0f}%")
    print(f"    Avg trail:    {pnls.mean():+.3f}%  net: {net.mean():+.3f}%")
    print(f"    Avg MFE:      {mfes.mean():+.3f}%")
    print(f"    Best hold:    {best_hold} = {best_avg:+.3f}%")
    if pnls.std() > 0:
        print(f"    Sharpe:       {net.mean() / pnls.std():.2f}")

    if "sell_pct" in trades[0]:
        print(f"    Avg sell%:    {np.mean([t['sell_pct'] for t in trades]):.1f}%")
        print(f"    Avg bar dur:  {np.mean([t['duration_s'] for t in trades]):.1f}s")

    if "imbalance" in trades[0]:
        imbs = [t["imbalance"] for t in trades]
        n_trades_list = [t.get("n_trades", 0) for t in trades]
        print(f"    Avg imbalance: {np.mean(imbs):+.0f} ticks")
        print(f"    Avg bar size:  {np.mean(n_trades_list):.0f} trades")
        # Imbalance direction vs trigger direction
        aligned = sum(1 for t in trades
                      if (t["trigger"] > 0 and t["imbalance"] > 0) or
                         (t["trigger"] < 0 and t["imbalance"] < 0))
        print(f"    Imb aligned:   {aligned}/{len(trades)} "
              f"({aligned/len(trades)*100:.0f}%)")
    print()


# ======================================================================
# Main
# ======================================================================

def main():
    print("=" * 70)
    print("  CASCADE TRIGGER: TIME vs TICK vs IMBALANCE BARS")
    print("=" * 70)
    print(f"\n  Tick bar: {TICK_SIZE} trades/bar")
    print(f"  Imbalance bar: init={IMBALANCE_INIT}, alpha={IMBALANCE_ALPHA}")
    print(f"  Lookback: {DAYS} days,  Fee: {FEE}%\n")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp() * 1000)

    all_time, all_tick, all_imb = [], [], []

    for sym in SYMBOLS:
        coin = sym.split("/")[0]
        print(f"\n{'='*40} {coin} {'='*40}")

        # Fetch trades
        print(f"  Fetching aggTrades...", end=" ", flush=True)
        raw_trades = fetch_agg_trades(sym, since_ms)
        print(f"{len(raw_trades):,} trades")
        if len(raw_trades) < TICK_SIZE * 30:
            print(f"  Not enough, skipping")
            continue

        # Build 3 bar types
        tick_bars = build_tick_bars(raw_trades, TICK_SIZE)
        imb_bars = build_imbalance_bars(raw_trades, IMBALANCE_INIT, IMBALANCE_ALPHA)
        time_bars = fetch_time_bars(sym, since_ms)

        print(f"  Time bars:      {len(time_bars)}")
        print(f"  Tick bars:      {len(tick_bars)} "
              f"(avg {np.mean([b['duration_s'] for b in tick_bars]):.0f}s)")
        print(f"  Imbalance bars: {len(imb_bars)} "
              f"(avg {np.mean([b['duration_s'] for b in imb_bars]):.0f}s, "
              f"avg size {np.mean([b['n_trades'] for b in imb_bars]):.0f} trades)")

        # Imbalance bar distribution analysis
        if imb_bars:
            durations = [b["duration_s"] for b in imb_bars]
            sizes = [b["n_trades"] for b in imb_bars]
            sell_pcts = [b["sell_pct"] for b in imb_bars]
            imbalances = [abs(b.get("imbalance", 0)) for b in imb_bars]

            print(f"\n  Imbalance bar profile:")
            print(f"    Duration: p10={np.percentile(durations, 10):.0f}s "
                  f"p50={np.percentile(durations, 50):.0f}s "
                  f"p90={np.percentile(durations, 90):.0f}s")
            print(f"    Size:     p10={np.percentile(sizes, 10):.0f} "
                  f"p50={np.percentile(sizes, 50):.0f} "
                  f"p90={np.percentile(sizes, 90):.0f} trades")
            print(f"    |Imb|:    p50={np.percentile(imbalances, 50):.0f} "
                  f"p90={np.percentile(imbalances, 90):.0f}")

            # Fast bars (< 30s) = high activity moments
            fast = [b for b in imb_bars if b["duration_s"] < 30]
            if fast:
                fast_sell = np.mean([b["sell_pct"] for b in fast])
                print(f"    Fast bars (<30s): {len(fast)} "
                      f"({len(fast)/len(imb_bars)*100:.0f}%), "
                      f"avg sell%={fast_sell:.0f}%")

        # Detect triggers on all 3
        time_trigs = detect_triggers(time_bars, HOLD_BARS)
        tick_trigs = detect_triggers(tick_bars, HOLD_BARS)
        imb_trigs = detect_triggers(imb_bars, HOLD_BARS)

        for t in time_trigs:
            t["sym"] = coin
        for t in tick_trigs:
            t["sym"] = coin
        for t in imb_trigs:
            t["sym"] = coin

        all_time.extend(time_trigs)
        all_tick.extend(tick_trigs)
        all_imb.extend(imb_trigs)

        print()
        print_stats(time_trigs, f"{coin} TIME (1m)")
        print_stats(tick_trigs, f"{coin} TICK ({TICK_SIZE})")
        print_stats(imb_trigs, f"{coin} IMBALANCE")

    # Overall
    print("\n" + "=" * 70)
    print("  OVERALL COMPARISON")
    print("=" * 70 + "\n")

    print_stats(all_time, "ALL — TIME BARS (1m)")
    print_stats(all_tick, f"ALL — TICK BARS ({TICK_SIZE})")
    print_stats(all_imb, "ALL — IMBALANCE BARS")

    # Summary table
    if all_time or all_tick or all_imb:
        print("\n  SUMMARY TABLE:")
        print(f"  {'Bar type':<20s} {'N':>4s} {'WR':>5s} {'Net':>8s} {'MFE':>8s} {'Sharpe':>7s}")
        print("  " + "-" * 55)
        for name, trades in [("Time (1m)", all_time),
                              (f"Tick ({TICK_SIZE})", all_tick),
                              ("Imbalance", all_imb)]:
            if trades:
                pnls = np.array([t["trailing"] for t in trades])
                mfes = np.array([t["mfe"] for t in trades])
                net = pnls - FEE
                wr = (pnls > 0).mean() * 100
                sh = net.mean() / pnls.std() if pnls.std() > 0 else 0
                print(f"  {name:<20s} {len(trades):>4d} {wr:>4.0f}% "
                      f"{net.mean():>+7.3f}% {mfes.mean():>+7.3f}% {sh:>7.2f}")
            else:
                print(f"  {name:<20s}    0     -        -        -       -")

    # Show imbalance triggers detail
    if all_imb:
        print(f"\n\n--- IMBALANCE BAR TRIGGERS (detail) ---\n")
        print(f"  {'Coin':>5s} {'Date':>14s} {'Trig':>7s} {'VolX':>5s} "
              f"{'Trail':>7s} {'MFE':>7s} {'Sell%':>6s} {'Imb':>6s} {'Dur':>6s}")
        print("  " + "-" * 68)
        for t in sorted(all_imb, key=lambda x: x["mfe"], reverse=True):
            imb = f"{t.get('imbalance', 0):+.0f}" if "imbalance" in t else "?"
            print(f"  {t['sym']:>5s} {t['date']:>14s} {t['trigger']:>+6.1f}% "
                  f"{t['vol_x']:>4.0f}x {t['trailing']:>+6.2f}% "
                  f"{t['mfe']:>+6.2f}% {t.get('sell_pct', 0):>5.0f}% "
                  f"{imb:>6s} {t.get('duration_s', 0):>5.0f}s")


if __name__ == "__main__":
    main()
