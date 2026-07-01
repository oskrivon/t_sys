"""Cascade tick bars backtest on Binance Vision historical aggTrades.

Downloads daily aggTrades ZIPs from data.binance.vision,
builds tick bars, runs cascade detection with exhaustion exits.

Usage:
    python scripts/research/cascade_tick_backtest.py [--days 90] [--coins ETH,SOL,DOGE]
"""
import argparse
import csv
import io
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import numpy as np

CACHE_DIR = "data/cache/aggtrades"
TICK_SIZE = 500
FEE = 0.08
MAX_HOLD = 20
COOLDOWN = 20

COINS = ["ETH", "SOL", "DOGE", "SUI", "AVAX", "LINK", "ARB", "WIF",
         "PEPE", "NEAR", "FIL", "APT"]

BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"


# ======================================================================
# Data download
# ======================================================================

def ensure_cached(symbol: str, date_str: str) -> Path | None:
    """Download aggTrades ZIP if not cached, return CSV path."""
    cache_path = Path(CACHE_DIR) / f"{symbol}-{date_str}.csv"

    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    url = f"{BASE_URL}/{symbol}USDT/{symbol}USDT-aggTrades-{date_str}.zip"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=30)
        data = resp.read()
    except HTTPError as e:
        if e.code != 404:
            print(f"    HTTP {e.code} for {symbol} {date_str}", flush=True)
        return None
    except Exception as e:
        print(f"    Error downloading {symbol} {date_str}: {e}", flush=True)
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        csv_name = zf.namelist()[0]
        csv_data = zf.read(csv_name)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(csv_data)
        return cache_path
    except Exception as e:
        print(f"    Error parsing {symbol} {date_str}: {e}", flush=True)
        return None


def stream_bars_from_csv(csv_path: Path, tick_size: int,
                         leftover: list) -> tuple[list[dict], list]:
    """Stream-build tick bars from CSV without loading all trades into memory.
    Returns (bars, leftover_trades)."""
    bucket = list(leftover)  # carry from previous day
    bars = []

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            try:
                bucket.append((
                    int(row[5]),         # ts
                    float(row[1]),       # price
                    float(row[2]),       # qty
                    row[6].strip().lower() == "true",  # is_sell
                ))
            except (ValueError, IndexError):
                continue

            if len(bucket) >= tick_size:
                chunk = bucket[:tick_size]
                prices = [t[1] for t in chunk]
                qty = sum(t[2] for t in chunk)
                sell_qty = sum(t[2] for t in chunk if t[3])
                bars.append({
                    "ts": chunk[0][0],
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "close": prices[-1],
                    "volume": qty,
                    "n_trades": tick_size,
                    "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
                    "duration_s": (chunk[-1][0] - chunk[0][0]) / 1000,
                })
                bucket = bucket[tick_size:]

    return bars, bucket


# ======================================================================
# Bar building & strategy (same as previous experiments)
# ======================================================================

def build_tick_bars(trades: list[tuple], tick_size: int) -> list[dict]:
    """Build tick bars from tuples (ts, price, qty, is_sell)."""
    bars = []
    for i in range(0, len(trades) - tick_size + 1, tick_size):
        chunk = trades[i:i + tick_size]
        prices = [t[1] for t in chunk]  # price
        qty = sum(t[2] for t in chunk)  # qty
        sell_qty = sum(t[2] for t in chunk if t[3])  # is_sell
        bars.append({
            "ts": chunk[0][0],  # ts
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": qty,
            "n_trades": tick_size,
            "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
            "duration_s": (chunk[-1][0] - chunk[0][0]) / 1000,  # ts
        })
    return bars


def exit_mom_fade(bars, entry_idx, direction):
    prev_abs_ret = abs(bars[entry_idx].get("ret", 1))
    fade_count = 0
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        curr_abs_ret = abs(bars[j].get("ret", 0))
        if curr_abs_ret < prev_abs_ret * 0.5:
            fade_count += 1
            if fade_count >= 2:
                return j
        else:
            fade_count = 0
        prev_abs_ret = curr_abs_ret
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_trailing(bars, entry_idx, direction, trail_pct=0.5):
    entry = bars[entry_idx]["close"]
    best = 0
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        if direction > 0:
            move = (bars[j]["high"] - entry) / entry * 100
            current = (bars[j]["close"] - entry) / entry * 100
        else:
            move = (entry - bars[j]["low"]) / entry * 100
            current = (entry - bars[j]["close"]) / entry * 100
        if move > best:
            best = move
        if best > trail_pct and best - current > trail_pct:
            return j
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_combined(bars, entry_idx, direction):
    """Exit when 2+ exhaustion signals."""
    trigger_dur = max(bars[entry_idx]["duration_s"], 1)
    prev_abs_ret = abs(bars[entry_idx].get("ret", 1))
    fade_count = 0
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        signals = 0
        if bars[j]["duration_s"] > trigger_dur * 2:
            signals += 1
        if direction < 0 and bars[j]["sell_pct"] < 40:
            signals += 1
        elif direction > 0 and bars[j]["sell_pct"] > 60:
            signals += 1
        curr_abs_ret = abs(bars[j].get("ret", 0))
        if curr_abs_ret < prev_abs_ret * 0.5:
            fade_count += 1
        else:
            fade_count = 0
        if fade_count >= 2:
            signals += 1
        prev_abs_ret = curr_abs_ret
        if signals >= 2:
            return j
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


EXIT_STRATEGIES = {
    "hold_10":    lambda bars, i, d: min(i + 10, len(bars) - 1),
    "trail_0.5%": lambda bars, i, d: exit_trailing(bars, i, d, 0.5),
    "mom_fade":   exit_mom_fade,
    "combined_2": exit_combined,
}


def run_backtest(bars: list[dict], sell_filter: float = 0) -> dict[str, list]:
    if len(bars) < 30:
        return {k: [] for k in EXIT_STRATEGIES}

    # Compute returns
    for i in range(1, len(bars)):
        bars[i]["ret"] = (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100

    avg_vol = np.mean([b["volume"] for b in bars[20:]])
    results = {name: [] for name in EXIT_STRATEGIES}
    cooldown = 0

    for i in range(20, len(bars) - MAX_HOLD - 5):
        if cooldown > 0:
            cooldown -= 1
            continue

        b = bars[i]
        if "ret" not in b:
            continue

        if abs(b["ret"]) > 0.5 and b["volume"] > avg_vol * 3:
            direction = 1 if b["ret"] > 0 else -1

            if sell_filter > 0:
                if direction < 0 and b["sell_pct"] < sell_filter:
                    continue
                if direction > 0 and (100 - b["sell_pct"]) < sell_filter:
                    continue

            entry = b["close"]
            dt = datetime.fromtimestamp(b["ts"] / 1000, tz=timezone.utc)

            for name, exit_fn in EXIT_STRATEGIES.items():
                exit_idx = exit_fn(bars, i, direction)
                exit_price = bars[exit_idx]["close"]
                pnl = direction * (exit_price - entry) / entry * 100

                mfe = 0
                mae = 0
                for j in range(i + 1, exit_idx + 1):
                    if direction > 0:
                        m = (bars[j]["high"] - entry) / entry * 100
                        d = (entry - bars[j]["low"]) / entry * 100
                    else:
                        m = (entry - bars[j]["low"]) / entry * 100
                        d = (bars[j]["high"] - entry) / entry * 100
                    mfe = max(mfe, m)
                    mae = max(mae, d)

                results[name].append({
                    "date": dt.strftime("%Y-%m-%d %H:%M"),
                    "sym": "",
                    "trigger": b["ret"],
                    "sell_pct": b["sell_pct"],
                    "dur_s": b["duration_s"],
                    "direction": direction,
                    "pnl": pnl,
                    "mfe": mfe,
                    "mae": mae,
                    "hold_bars": exit_idx - i,
                    "capture": pnl / mfe * 100 if mfe > 0 else 0,
                })

            cooldown = COOLDOWN

    return results


# ======================================================================
# Main
# ======================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--coins", type=str, default=",".join(COINS))
    args = parser.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",")]
    days = args.days

    print("=" * 70)
    print("  CASCADE TICK BARS — FULL BACKTEST")
    print("=" * 70)
    print(f"  Period: {days} days,  Coins: {len(coins)}")
    print(f"  Tick size: {TICK_SIZE},  Fee: {FEE}%")
    print(f"  Source: data.binance.vision aggTrades\n")

    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)
    dates = [(start_date + timedelta(days=d)).strftime("%Y-%m-%d")
             for d in range(days)]

    # Aggregate
    agg_nf = {k: [] for k in EXIT_STRATEGIES}  # no filter
    agg_sf = {k: [] for k in EXIT_STRATEGIES}  # sell% filter

    for coin in coins:
        print(f"\n{'='*30} {coin} {'='*30}", flush=True)

        # Stream tick bars day-by-day (memory: only bars + tiny leftover)
        all_bars = []
        leftover = []
        downloaded = 0

        for di, date_str in enumerate(dates):
            csv_path = ensure_cached(coin, date_str)
            if csv_path is None:
                continue

            day_bars, leftover = stream_bars_from_csv(csv_path, TICK_SIZE, leftover)
            all_bars.extend(day_bars)
            downloaded += 1

            if (di + 1) % 15 == 0:
                print(f"  {di+1}/{len(dates)} days, {len(all_bars):,} bars...",
                      flush=True)

        print(f"  {downloaded} days, {len(all_bars):,} tick bars", flush=True)

        if len(all_bars) < 50:
            print(f"  Not enough bars, skipping")
            continue

        # Run backtest on all bars
        res = run_backtest(all_bars, sell_filter=0)
        for name in EXIT_STRATEGIES:
            for t in res[name]:
                t["sym"] = coin
            agg_nf[name].extend(res[name])

        res_f = run_backtest(all_bars, sell_filter=80)
        for name in EXIT_STRATEGIES:
            for t in res_f[name]:
                t["sym"] = coin
            agg_sf[name].extend(res_f[name])

        n = len(res["mom_fade"])
        nf = len(res_f["mom_fade"])
        print(f"  Triggers: {n} (no filter), {nf} (sell%>80%)", flush=True)

        if res_f["mom_fade"]:
            pnls = [t["pnl"] for t in res_f["mom_fade"]]
            print(f"  mom_fade+filter: WR={sum(1 for p in pnls if p > 0)/len(pnls)*100:.0f}% "
                  f"avg={np.mean(pnls):+.3f}% n={len(pnls)}", flush=True)

        del all_bars

    # ======================================================================
    # Overall Results
    # ======================================================================

    for filter_name, agg in [("NO FILTER", agg_nf),
                              ("SELL% > 80%", agg_sf)]:
        print(f"\n\n{'='*70}")
        print(f"  RESULTS — {filter_name} — {days} DAYS, {len(coins)} COINS")
        print(f"{'='*70}\n")

        print(f"  {'Strategy':<13s} {'N':>4s} {'WR':>5s} {'Avg':>8s} {'Net':>8s} "
              f"{'MFE':>7s} {'MAE':>7s} {'Capt':>6s} {'Sharpe':>7s}")
        print("  " + "-" * 68)

        for name in EXIT_STRATEGIES:
            trades = agg[name]
            if not trades:
                print(f"  {name:<13s}    0     -        -        -"
                      f"       -       -      -       -")
                continue

            pnls = np.array([t["pnl"] for t in trades])
            mfes = np.array([t["mfe"] for t in trades])
            maes = np.array([t["mae"] for t in trades])
            captures = np.array([t["capture"] for t in trades])
            net = pnls - FEE
            wr = (pnls > 0).mean() * 100
            sh = net.mean() / pnls.std() * np.sqrt(len(trades) / (days / 365)) if pnls.std() > 0 else 0
            # Per-trade sharpe (not annualized)
            sh_trade = net.mean() / pnls.std() if pnls.std() > 0 else 0

            print(f"  {name:<13s} {len(trades):>4d} {wr:>4.0f}% {pnls.mean():>+7.3f}% "
                  f"{net.mean():>+7.3f}% {mfes.mean():>+6.3f}% {maes.mean():>+6.3f}% "
                  f"{captures.mean():>5.0f}% {sh:>7.2f}")

    # Annualized estimates for best strategy
    print(f"\n\n{'='*70}")
    print(f"  ANNUALIZED ESTIMATES")
    print(f"{'='*70}\n")

    for filter_name, agg in [("no filter", agg_nf), ("sell%>80%", agg_sf)]:
        best_name = max(EXIT_STRATEGIES.keys(),
                        key=lambda n: np.mean([t["pnl"] for t in agg[n]]) - FEE
                        if agg[n] else -999)
        trades = agg[best_name]
        if not trades:
            continue

        pnls = np.array([t["pnl"] for t in trades])
        net = pnls - FEE
        n = len(trades)
        trades_per_day = n / days
        trades_per_year = trades_per_day * 365

        annual_ret = trades_per_year * net.mean()
        annual_sharpe = net.mean() / pnls.std() * np.sqrt(trades_per_year) if pnls.std() > 0 else 0

        # Max drawdown from equity curve
        cumulative = np.cumsum(net)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        max_dd = drawdowns.max()

        # Profit factor
        wins = net[net > 0]
        losses = net[net < 0]
        pf = abs(wins.sum() / losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')

        print(f"  Best strategy ({filter_name}): {best_name}")
        print(f"    Trades: {n} in {days}d = {trades_per_day:.2f}/day")
        print(f"    WR: {(pnls > 0).mean()*100:.1f}%")
        print(f"    Avg net/trade: {net.mean():+.3f}%")
        print(f"    Std/trade: {pnls.std():.3f}%")
        print(f"    Profit factor: {pf:.2f}")
        print(f"    Max DD (cum): {max_dd:.2f}%")
        print(f"    ---")
        print(f"    Annual return (1x): {annual_ret:+.1f}%")
        print(f"    Annualized Sharpe: {annual_sharpe:.2f}")

        # Slippage scenarios
        for slip in [0.03, 0.05, 0.10]:
            adj_net = net.mean() - slip * 2
            adj_annual = trades_per_year * adj_net
            print(f"    w/ {slip:.2f}% slip/side: annual {adj_annual:+.1f}%, "
                  f"net/trade {adj_net:+.3f}%")
        print()

    # Monthly breakdown (for best filtered strategy)
    best_name = max(EXIT_STRATEGIES.keys(),
                    key=lambda n: np.mean([t["pnl"] for t in agg_sf[n]]) - FEE
                    if agg_sf[n] else -999)
    trades = agg_sf[best_name]
    if trades and len(trades) > 5:
        print(f"\n  MONTHLY BREAKDOWN — {best_name} + sell%>80%:\n")
        from collections import defaultdict
        monthly = defaultdict(list)
        for t in trades:
            month = t["date"][:7]
            monthly[month].append(t["pnl"] - FEE)

        print(f"  {'Month':<10s} {'N':>4s} {'WR':>5s} {'Avg net':>8s} {'Total':>8s}")
        print("  " + "-" * 38)
        for month in sorted(monthly.keys()):
            pnls = monthly[month]
            wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            print(f"  {month:<10s} {len(pnls):>4d} {wr:>4.0f}% "
                  f"{np.mean(pnls):>+7.3f}% {sum(pnls):>+7.2f}%")

    # Per-coin breakdown
    if trades and len(trades) > 5:
        print(f"\n  PER-COIN BREAKDOWN — {best_name} + sell%>80%:\n")
        from collections import defaultdict
        by_coin = defaultdict(list)
        for t in trades:
            by_coin[t["sym"]].append(t["pnl"] - FEE)

        print(f"  {'Coin':<8s} {'N':>4s} {'WR':>5s} {'Avg net':>8s} {'Total':>8s}")
        print("  " + "-" * 38)
        for coin in sorted(by_coin.keys(), key=lambda c: sum(by_coin[c]), reverse=True):
            pnls = by_coin[coin]
            wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            print(f"  {coin:<8s} {len(pnls):>4d} {wr:>4.0f}% "
                  f"{np.mean(pnls):>+7.3f}% {sum(pnls):>+7.2f}%")


if __name__ == "__main__":
    main()
