"""Informed flow detection via tick bar anomalies.

Hypothesis: when large volume flows through without moving price,
an informed player is accumulating/distributing. Price eventually
moves in their direction.

Signals detected:
  1. ABSORPTION: high volume + low price impact + directional sell%
     (someone absorbs selling/buying pressure without price moving)
  2. BREAKOUT: sudden move after period of absorption
     (accumulation phase ends, aggressive phase begins)
  3. DIVERGENCE: sell% says one thing, price does opposite
     (hidden buyer/seller masking their activity)

Uses Binance Vision aggTrades, streams through CSV.

Usage:
    python scripts/research/informed_flow_detector.py [--days 90]
"""
import csv
import io
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import numpy as np

ROOT = Path("/root/trading")
CACHE_DIR = ROOT / "data" / "cache" / "aggtrades"
BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"

SYMBOLS = ["SOL", "DOGE", "SUI", "AVAX", "LINK"]
TICK_SIZE = 500
FEE = 0.08  # roundtrip %


def ensure_cached(symbol: str, date_str: str) -> Path | None:
    cache_path = CACHE_DIR / f"{symbol}-{date_str}.csv"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    url = f"{BASE_URL}/{symbol}USDT/{symbol}USDT-aggTrades-{date_str}.zip"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=30)
        data = resp.read()
    except (HTTPError, Exception):
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        csv_data = zf.read(zf.namelist()[0])
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(csv_data)
        return cache_path
    except Exception:
        return None


def stream_tick_bars(csv_path: Path, tick_size: int, leftover: list) -> tuple[list[dict], list]:
    """Stream tick bars from CSV. Returns (bars, leftover)."""
    bucket = list(leftover)
    bars = []

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            try:
                bucket.append((
                    int(row[5]),
                    float(row[1]),
                    float(row[2]),
                    row[6].strip().lower() == "true",
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
                    "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
                    "duration_s": (chunk[-1][0] - chunk[0][0]) / 1000,
                    "ret": (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0,
                    "range_pct": (max(prices) - min(prices)) / prices[0] * 100 if prices[0] > 0 else 0,
                })
                bucket = bucket[tick_size:]

    return bars, bucket


def compute_features(bars: list[dict], lookback: int = 20):
    """Add rolling features to bars."""
    for i in range(lookback, len(bars)):
        window = bars[i - lookback : i]
        b = bars[i]

        avg_vol = np.mean([w["volume"] for w in window])
        avg_range = np.mean([w["range_pct"] for w in window])
        avg_dur = np.mean([w["duration_s"] for w in window])
        avg_abs_ret = np.mean([abs(w["ret"]) for w in window])

        b["vol_ratio"] = b["volume"] / avg_vol if avg_vol > 0 else 1
        b["impact"] = abs(b["ret"]) / b["vol_ratio"] if b["vol_ratio"] > 0 else 0
        b["range_ratio"] = b["range_pct"] / avg_range if avg_range > 0 else 1
        b["dur_ratio"] = b["duration_s"] / avg_dur if avg_dur > 0 else 1

        # Price impact per unit volume: how much does price move per $ traded
        # Low = someone absorbing, High = normal or panic
        b["price_impact"] = abs(b["ret"]) / (b["volume"] + 1e-10)
        avg_pi = np.mean([abs(w["ret"]) / (w["volume"] + 1e-10) for w in window])
        b["pi_ratio"] = b["price_impact"] / avg_pi if avg_pi > 0 else 1

        # Sell% deviation from neutral
        b["sell_deviation"] = b["sell_pct"] - 50

        # Divergence: sell pressure vs price direction
        # Positive = sell pressure but price up (hidden buyer)
        # Negative = buy pressure but price down (hidden seller)
        b["divergence"] = b["sell_deviation"] * (-1 if b["ret"] > 0 else 1) if abs(b["ret"]) > 0.01 else 0


def detect_signals(bars: list[dict], lookback: int = 20) -> list[dict]:
    """Detect informed flow signals."""
    if len(bars) < lookback + 30:
        return []

    compute_features(bars, lookback)
    signals = []
    cooldown = 0

    for i in range(lookback + 5, len(bars) - 20):
        if cooldown > 0:
            cooldown -= 1
            continue

        b = bars[i]
        if "vol_ratio" not in b:
            continue

        signal = None

        # --- SIGNAL 1: ABSORPTION ---
        # High volume, low price impact, directional sell%
        if (b["vol_ratio"] > 2.0 and
            b["pi_ratio"] < 0.3 and
            abs(b["sell_deviation"]) > 10):

            # Direction: opposite of flow (absorber wins)
            if b["sell_deviation"] > 10:
                direction = 1   # selling absorbed → hidden buyer → price up
            else:
                direction = -1  # buying absorbed → hidden seller → price down

            signal = {
                "type": "ABSORPTION",
                "direction": direction,
                "strength": b["vol_ratio"] * (1 / max(b["pi_ratio"], 0.01)),
                "detail": f"vol={b['vol_ratio']:.1f}x pi={b['pi_ratio']:.2f} sell={b['sell_pct']:.0f}%",
            }

        # --- SIGNAL 2: STEALTH DIVERGENCE ---
        # Sustained: last 3 bars have sell%>60 but price flat or up
        if signal is None and i >= lookback + 3:
            last3 = bars[i-2:i+1]
            avg_sell_dev = np.mean([bb.get("sell_deviation", 0) for bb in last3])
            avg_ret = np.mean([bb.get("ret", 0) for bb in last3])
            avg_vol_r = np.mean([bb.get("vol_ratio", 1) for bb in last3])

            # Selling but price stable/up = hidden buyer
            if avg_sell_dev > 8 and avg_ret > -0.05 and avg_vol_r > 1.5:
                signal = {
                    "type": "STEALTH_BUY",
                    "direction": 1,
                    "strength": avg_sell_dev * avg_vol_r,
                    "detail": f"3bar sell_dev={avg_sell_dev:.1f} ret={avg_ret:+.2f}% vol={avg_vol_r:.1f}x",
                }

            # Buying but price stable/down = hidden seller
            elif avg_sell_dev < -8 and avg_ret < 0.05 and avg_vol_r > 1.5:
                signal = {
                    "type": "STEALTH_SELL",
                    "direction": -1,
                    "strength": abs(avg_sell_dev) * avg_vol_r,
                    "detail": f"3bar sell_dev={avg_sell_dev:.1f} ret={avg_ret:+.2f}% vol={avg_vol_r:.1f}x",
                }

        # --- SIGNAL 3: POST-QUIET BREAKOUT ---
        # Low volatility for 5+ bars then sudden move with volume
        if signal is None:
            quiet_bars = bars[i-5:i]
            if all("vol_ratio" in bb for bb in quiet_bars):
                quiet_rets = [abs(bb["ret"]) for bb in quiet_bars]
                quiet_avg_ret = np.mean(quiet_rets)

                if (quiet_avg_ret < 0.1 and
                    abs(b["ret"]) > 0.4 and
                    b["vol_ratio"] > 2.0):

                    direction = 1 if b["ret"] > 0 else -1
                    signal = {
                        "type": "BREAKOUT",
                        "direction": direction,
                        "strength": abs(b["ret"]) / max(quiet_avg_ret, 0.01) * b["vol_ratio"],
                        "detail": f"quiet={quiet_avg_ret:.3f}% break={b['ret']:+.2f}% vol={b['vol_ratio']:.1f}x",
                    }

        if signal is None:
            continue

        # --- Measure outcome ---
        entry = b["close"]
        direction = signal["direction"]
        dt = datetime.fromtimestamp(b["ts"] / 1000, tz=timezone.utc)

        outcomes = {}
        for hold in [5, 10, 20]:
            idx = min(i + hold, len(bars) - 1)
            exit_p = bars[idx]["close"]
            pnl = direction * (exit_p - entry) / entry * 100
            outcomes[f"h{hold}"] = pnl

        # MFE / MAE
        mfe, mae = 0, 0
        for j in range(i + 1, min(i + 21, len(bars))):
            if direction > 0:
                m = (bars[j]["high"] - entry) / entry * 100
                d = (entry - bars[j]["low"]) / entry * 100
            else:
                m = (entry - bars[j]["low"]) / entry * 100
                d = (bars[j]["high"] - entry) / entry * 100
            mfe = max(mfe, m)
            mae = max(mae, d)

        signals.append({
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "type": signal["type"],
            "dir": "LONG" if direction > 0 else "SHORT",
            "strength": signal["strength"],
            "detail": signal["detail"],
            "sell_pct": b["sell_pct"],
            "dur_s": b["duration_s"],
            **outcomes,
            "mfe": mfe,
            "mae": mae,
        })
        cooldown = 15

    return signals


def print_results(all_signals: list[dict], days: int):
    """Print analysis of detected signals."""
    if not all_signals:
        print("\n  No signals detected!")
        return

    print(f"\n{'='*70}")
    print(f"  INFORMED FLOW DETECTION — {len(all_signals)} SIGNALS, {days} DAYS")
    print(f"{'='*70}\n")

    # By signal type
    types = sorted(set(s["type"] for s in all_signals))

    print(f"  {'Type':<15s} {'N':>4s} {'WR_h5':>6s} {'WR_h10':>7s} {'WR_h20':>7s} "
          f"{'Net_h10':>8s} {'MFE':>7s} {'MAE':>7s} {'MFE/MAE':>8s}")
    print("  " + "-" * 72)

    for sig_type in types:
        group = [s for s in all_signals if s["type"] == sig_type]
        n = len(group)

        for hold_key in ["h5", "h10", "h20"]:
            pnls = [s[hold_key] for s in group]

        wr5 = sum(1 for s in group if s["h5"] > 0) / n * 100
        wr10 = sum(1 for s in group if s["h10"] > 0) / n * 100
        wr20 = sum(1 for s in group if s["h20"] > 0) / n * 100
        avg_h10 = np.mean([s["h10"] for s in group])
        avg_mfe = np.mean([s["mfe"] for s in group])
        avg_mae = np.mean([s["mae"] for s in group])
        ratio = avg_mfe / avg_mae if avg_mae > 0 else 0

        print(f"  {sig_type:<15s} {n:>4d} {wr5:>5.0f}% {wr10:>6.0f}% {wr20:>6.0f}% "
              f"{avg_h10 - FEE:>+7.3f}% {avg_mfe:>+6.3f}% {avg_mae:>+6.3f}% {ratio:>7.2f}")

    # Overall
    print()
    n = len(all_signals)
    wr10 = sum(1 for s in all_signals if s["h10"] > 0) / n * 100
    avg_h10 = np.mean([s["h10"] for s in all_signals])
    avg_mfe = np.mean([s["mfe"] for s in all_signals])
    avg_mae = np.mean([s["mae"] for s in all_signals])

    print(f"  {'TOTAL':<15s} {n:>4d}        {wr10:>6.0f}%         "
          f"{avg_h10 - FEE:>+7.3f}% {avg_mfe:>+6.3f}% {avg_mae:>+6.3f}% "
          f"{avg_mfe/avg_mae if avg_mae > 0 else 0:>7.2f}")

    # Key metric: MFE vs MAE
    print(f"\n  KEY: MFE/MAE ratio > 1.0 means directional edge exists")
    print(f"       MFE/MAE = 1.0 means random (no edge)")

    # Strength filter
    print(f"\n\n  STRENGTH FILTER (top quartile vs all):\n")
    strengths = sorted([s["strength"] for s in all_signals])
    q75 = np.percentile(strengths, 75)

    strong = [s for s in all_signals if s["strength"] >= q75]
    weak = [s for s in all_signals if s["strength"] < q75]

    for label, group in [("Strong (top 25%)", strong), ("Weak (bottom 75%)", weak)]:
        if not group:
            continue
        n = len(group)
        wr10 = sum(1 for s in group if s["h10"] > 0) / n * 100
        avg_h10 = np.mean([s["h10"] for s in group])
        avg_mfe = np.mean([s["mfe"] for s in group])
        avg_mae = np.mean([s["mae"] for s in group])
        ratio = avg_mfe / avg_mae if avg_mae > 0 else 0
        print(f"  {label:<25s}: N={n:>3d} WR={wr10:.0f}% net={avg_h10 - FEE:+.3f}% "
              f"MFE/MAE={ratio:.2f}")

    # By direction
    print(f"\n\n  BY DIRECTION:\n")
    for d in ["LONG", "SHORT"]:
        group = [s for s in all_signals if s["dir"] == d]
        if not group:
            continue
        n = len(group)
        wr10 = sum(1 for s in group if s["h10"] > 0) / n * 100
        avg_h10 = np.mean([s["h10"] for s in group])
        avg_mfe = np.mean([s["mfe"] for s in group])
        avg_mae = np.mean([s["mae"] for s in group])
        print(f"  {d:<6s}: N={n:>3d} WR={wr10:.0f}% net={avg_h10 - FEE:+.3f}% "
              f"MFE={avg_mfe:+.3f}% MAE={avg_mae:+.3f}%")

    # Show top signals by outcome
    print(f"\n\n  TOP SIGNALS (by h10 PnL):\n")
    print(f"  {'Sym':>5s} {'Date':>14s} {'Type':<15s} {'Dir':<5s} "
          f"{'h5':>7s} {'h10':>7s} {'h20':>7s} {'MFE':>7s} {'Detail'}")
    print("  " + "-" * 90)
    for s in sorted(all_signals, key=lambda x: x["h10"], reverse=True)[:15]:
        print(f"  {s.get('sym','?'):>5s} {s['date']:>14s} {s['type']:<15s} {s['dir']:<5s} "
              f"{s['h5']:>+6.2f}% {s['h10']:>+6.2f}% {s['h20']:>+6.2f}% "
              f"{s['mfe']:>+6.2f}% {s['detail']}")

    print(f"\n  WORST SIGNALS:")
    print("  " + "-" * 90)
    for s in sorted(all_signals, key=lambda x: x["h10"])[:10]:
        print(f"  {s.get('sym','?'):>5s} {s['date']:>14s} {s['type']:<15s} {s['dir']:<5s} "
              f"{s['h5']:>+6.2f}% {s['h10']:>+6.2f}% {s['h20']:>+6.2f}% "
              f"{s['mfe']:>+6.2f}% {s['detail']}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    days = args.days

    print("=" * 70)
    print("  INFORMED FLOW DETECTOR — TICK BAR ANOMALIES")
    print("=" * 70)
    print(f"  Days: {days},  Coins: {SYMBOLS}")
    print(f"  Tick size: {TICK_SIZE}\n")
    print(f"  Signals:")
    print(f"    ABSORPTION:   vol>2x + price_impact<0.3x + |sell_dev|>10")
    print(f"    STEALTH_BUY:  3 bars sell%>58 + price flat/up + vol>1.5x")
    print(f"    STEALTH_SELL: 3 bars sell%<42 + price flat/down + vol>1.5x")
    print(f"    BREAKOUT:     5 bars quiet (<0.1%) then |ret|>0.4% + vol>2x")
    print()

    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)
    dates = [(start_date + timedelta(days=d)).strftime("%Y-%m-%d")
             for d in range(days)]

    all_signals = []

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---", flush=True)

        all_bars = []
        leftover = []

        for di, date_str in enumerate(dates):
            csv_path = ensure_cached(symbol, date_str)
            if csv_path is None:
                continue
            day_bars, leftover = stream_tick_bars(csv_path, TICK_SIZE, leftover)
            all_bars.extend(day_bars)

            if (di + 1) % 30 == 0:
                print(f"  {di+1}/{len(dates)} days, {len(all_bars):,} bars...", flush=True)

        print(f"  {len(all_bars):,} tick bars", flush=True)

        if len(all_bars) < 100:
            continue

        sigs = detect_signals(all_bars, lookback=20)
        for s in sigs:
            s["sym"] = symbol
        all_signals.extend(sigs)

        print(f"  Signals: {len(sigs)} "
              f"({sum(1 for s in sigs if s['type']=='ABSORPTION')} abs, "
              f"{sum(1 for s in sigs if 'STEALTH' in s['type'])} stealth, "
              f"{sum(1 for s in sigs if s['type']=='BREAKOUT')} break)", flush=True)

        del all_bars

    print_results(all_signals, days)


if __name__ == "__main__":
    main()
