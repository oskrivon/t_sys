#!/usr/bin/env python3
"""V-Bottom filter backtest — test crash protection filters.

Compares baseline V-bottom (drop≥3%, NATR>median, hold 24h) against
filtered versions:
  F1: Regime vol_ratio > 1.8 → skip
  F2: BTC down >5% in 7d → skip
  F3: Cooldown 48h after losing trade → skip
  F4: Kalman velocity < 0 → skip long

Fetches BTC/USDT 1h from Bybit, 2024-01 to present.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ── Data fetch ──────────────────────────────────────────────────────

def fetch_all_1h(symbol: str = "BTCUSDT",
                 start: str = "2024-01-01") -> list[dict]:
    """Fetch full 1h history from Binance Futures."""
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    since = int(datetime.fromisoformat(start).replace(
        tzinfo=timezone.utc).timestamp() * 1000)
    all_data = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        if not batch:
            break
        all_data.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)
        if len(all_data) % 5000 == 0:
            print(f"  ... fetched {len(all_data)} candles")
    # dedupe
    seen = set()
    clean = []
    for c in all_data:
        if c[0] not in seen:
            seen.add(c[0])
            clean.append(c)
    clean.sort(key=lambda x: x[0])
    print(f"Fetched {len(clean)} candles "
          f"({datetime.fromtimestamp(clean[0][0]/1000, tz=timezone.utc).date()} → "
          f"{datetime.fromtimestamp(clean[-1][0]/1000, tz=timezone.utc).date()})")
    return clean


# ── Indicators ──────────────────────────────────────────────────────

def compute_natr(highs, lows, closes, idx, period=14) -> float:
    if idx < period + 1:
        return 0.0
    trs = []
    for i in range(idx - period, idx):
        tr = max(highs[i+1] - lows[i+1],
                 abs(highs[i+1] - closes[i]),
                 abs(lows[i+1] - closes[i]))
        trs.append(tr)
    atr = sum(trs) / period
    return atr / closes[idx] * 100 if closes[idx] > 0 else 0


def compute_vol_ratio(closes, idx, short=10, long=60) -> float:
    """Regime-style vol_ratio: std(recent 10) / std(full 60)."""
    if idx < long:
        return 1.0
    rets = np.diff(closes[idx - long:idx + 1]) / closes[idx - long:idx]
    if len(rets) < short + 1:
        return 1.0
    recent_vol = np.std(rets[-short:])
    avg_vol = np.std(rets)
    return float(recent_vol / avg_vol) if avg_vol > 0 else 1.0


def compute_kalman_velocity(closes, idx, lookback=100) -> float:
    """Simple Kalman filter — return velocity (price derivative)."""
    start = max(0, idx - lookback)
    prices = closes[start:idx + 1]
    if len(prices) < 20:
        return 0.0

    # 2-state Kalman: [price, velocity]
    x = np.array([prices[0], 0.0])  # state
    P = np.diag([1.0, 1.0])         # covariance
    F = np.array([[1.0, 1.0], [0.0, 1.0]])  # transition
    H = np.array([[1.0, 0.0]])      # observation
    Q = np.diag([0.01, 0.001])      # process noise
    R = np.array([[1.0]])           # measurement noise

    for p in prices[1:]:
        # predict
        x = F @ x
        P = F @ P @ F.T + Q
        # update
        y = p - H @ x
        S = H @ P @ H.T + R
        K = P @ H.T @ np.linalg.inv(S)
        x = x + (K @ y).flatten()
        P = (np.eye(2) - K @ H) @ P

    return float(x[1])  # velocity


def weekly_return(closes, idx, hours=168) -> float:
    """Return over last 7 days (168 hours)."""
    if idx < hours:
        return 0.0
    return (closes[idx] - closes[idx - hours]) / closes[idx - hours] * 100


# ── Backtest engine ─────────────────────────────────────────────────

def run_backtest(candles: list[dict], filters: dict) -> dict:
    """Run V-bottom backtest with optional filters.

    filters dict keys (all bool):
        vol_regime: skip if vol_ratio > 1.8
        weekly_trend: skip if BTC down >5% in 7d
        cooldown: skip if last trade was a loss < 48h ago
        kalman: skip if kalman velocity < 0
    """
    closes = np.array([c[4] for c in candles], dtype=float)
    highs = np.array([c[2] for c in candles], dtype=float)
    lows = np.array([c[3] for c in candles], dtype=float)
    timestamps = [c[0] for c in candles]
    n = len(closes)

    # Pre-compute NATR median (rolling, min 200 samples)
    natr_history = []
    natr_medians = np.zeros(n)
    for i in range(30, n):
        natr_val = compute_natr(highs, lows, closes, i)
        natr_history.append(natr_val)
        if len(natr_history) > 5000:
            natr_history = natr_history[-5000:]
        natr_medians[i] = np.median(natr_history) if len(natr_history) > 50 else 0

    trades = []
    in_position = False
    entry_idx = 0
    last_exit_ts = 0
    last_trade_pnl = 0.0

    for i in range(168, n - 24):  # need 7d lookback + 24h forward
        if in_position:
            # Check 24h exit
            if i - entry_idx >= 24:
                exit_price = closes[i]
                pnl = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "entry_ts": timestamps[entry_idx],
                    "drop_pct": entry_drop,
                })
                in_position = False
                last_exit_ts = timestamps[i]
                last_trade_pnl = pnl
            continue

        # ── Check entry signal ──
        # Drop detection: max close in last 24h vs current
        window = closes[i - 24:i + 1]
        window_high = np.max(window)
        drop_pct = (window_high - closes[i]) / window_high * 100

        if drop_pct < 3.0:
            continue

        # NATR filter
        natr = compute_natr(highs, lows, closes, i)
        if natr_medians[i] > 0 and natr <= natr_medians[i]:
            continue

        # ── Apply crash protection filters ──
        skipped = False
        skip_reason = ""

        # F1: Vol regime
        if filters.get("vol_regime"):
            vr = compute_vol_ratio(closes, i)
            if vr > 1.8:
                skipped = True
                skip_reason = "vol_regime"

        # F2: Weekly trend
        if not skipped and filters.get("weekly_trend"):
            wr = weekly_return(closes, i)
            if wr < -5.0:
                skipped = True
                skip_reason = "weekly_trend"

        # F3: Cooldown after loss
        if not skipped and filters.get("cooldown"):
            if last_exit_ts > 0 and last_trade_pnl < 0:
                hours_since = (timestamps[i] - last_exit_ts) / 3600000
                if hours_since < 48:
                    skipped = True
                    skip_reason = "cooldown"

        # F4: Kalman velocity
        if not skipped and filters.get("kalman"):
            vel = compute_kalman_velocity(closes, i)
            if vel < 0:
                skipped = True
                skip_reason = "kalman"

        if skipped:
            continue

        # ── Enter ──
        entry_price = closes[i]
        entry_drop = drop_pct
        entry_idx = i
        in_position = True

    # ── Stats ──
    if not trades:
        return {"n": 0, "wr": 0, "avg": 0, "total": 0, "max_dd": 0,
                "trades": [], "filters": filters}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Max drawdown (cumulative)
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    max_dd = float(np.min(dd))

    # Consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for p in pnls:
        if p <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    return {
        "n": len(pnls),
        "wr": len(wins) / len(pnls) * 100,
        "avg": np.mean(pnls),
        "avg_win": np.mean(wins) if wins else 0,
        "avg_loss": np.mean(losses) if losses else 0,
        "total": sum(pnls),
        "max_dd": max_dd,
        "max_consec_loss": max_consec_loss,
        "sharpe": float(np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls) / 2.5))
                  if np.std(pnls) > 0 else 0,
        "trades": trades,
        "filters": filters,
    }


# ── Main ────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("V-BOTTOM FILTER BACKTEST")
    print("=" * 70)

    candles = fetch_all_1h()

    configs = {
        "BASELINE (no filters)": {},
        "F1: vol_regime only": {"vol_regime": True},
        "F2: weekly_trend only": {"weekly_trend": True},
        "F3: cooldown only": {"cooldown": True},
        "F4: kalman only": {"kalman": True},
        "F1+F2: vol + trend": {"vol_regime": True, "weekly_trend": True},
        "F1+F2+F3: vol+trend+cool": {"vol_regime": True, "weekly_trend": True, "cooldown": True},
        "F2+F3: trend+cooldown": {"weekly_trend": True, "cooldown": True},
        "ALL: F1+F2+F3+F4": {"vol_regime": True, "weekly_trend": True,
                              "cooldown": True, "kalman": True},
    }

    results = {}
    for name, filt in configs.items():
        print(f"\nRunning: {name}...")
        res = run_backtest(candles, filt)
        results[name] = res
        print(f"  Trades: {res['n']}  WR: {res['wr']:.0f}%  "
              f"Avg: {res['avg']:+.2f}%  Total: {res['total']:+.1f}%  "
              f"MaxDD: {res['max_dd']:.1f}%  Sharpe: {res['sharpe']:.2f}")

    # ── Summary table ──
    print("\n" + "=" * 70)
    print(f"{'Config':<30} {'N':>4} {'WR':>5} {'Avg':>7} {'Total':>8} "
          f"{'MaxDD':>7} {'Sharpe':>7} {'ConsL':>5}")
    print("-" * 70)

    baseline = results["BASELINE (no filters)"]
    for name, res in results.items():
        marker = ""
        if res["total"] > baseline["total"] and name != "BASELINE (no filters)":
            marker = " ✓"
        print(f"{name:<30} {res['n']:>4} {res['wr']:>4.0f}% "
              f"{res['avg']:>+6.2f}% {res['total']:>+7.1f}% "
              f"{res['max_dd']:>6.1f}% {res['sharpe']:>7.2f} "
              f"{res.get('max_consec_loss', 0):>4}{marker}")

    # ── Filtered-out trades analysis ──
    print("\n" + "=" * 70)
    print("FILTERED-OUT TRADES ANALYSIS")
    print("=" * 70)

    base_trades = results["BASELINE (no filters)"]["trades"]

    for name, res in results.items():
        if not res["filters"]:
            continue
        kept_entries = {t["entry_idx"] for t in res["trades"]}
        filtered = [t for t in base_trades if t["entry_idx"] not in kept_entries]
        if not filtered:
            continue
        f_pnls = [t["pnl"] for t in filtered]
        f_wins = sum(1 for p in f_pnls if p > 0)
        print(f"\n{name}:")
        print(f"  Filtered out {len(filtered)} trades, "
              f"their avg PnL: {np.mean(f_pnls):+.2f}%, "
              f"WR: {f_wins/len(filtered)*100:.0f}%")
        for t in filtered:
            dt = datetime.fromtimestamp(t["entry_ts"]/1000, tz=timezone.utc)
            print(f"    {dt.strftime('%Y-%m-%d %H:%M')} "
                  f"entry=${t['entry_price']:,.0f} "
                  f"pnl={t['pnl']:+.2f}% "
                  f"drop={t['drop_pct']:.1f}%")

    # ── Recent crash detail ──
    print("\n" + "=" * 70)
    print("RECENT TRADES (2026)")
    print("=" * 70)
    for name in ["BASELINE (no filters)", "F1+F2+F3: vol+trend+cool", "ALL: F1+F2+F3+F4"]:
        res = results[name]
        recent = [t for t in res["trades"]
                  if t["entry_ts"] > datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000]
        if recent:
            print(f"\n{name}:")
            for t in recent:
                dt = datetime.fromtimestamp(t["entry_ts"]/1000, tz=timezone.utc)
                print(f"  {dt.strftime('%Y-%m-%d %H:%M')} "
                      f"${t['entry_price']:,.0f} → ${t['exit_price']:,.0f} "
                      f"pnl={t['pnl']:+.2f}%")


if __name__ == "__main__":
    main()
