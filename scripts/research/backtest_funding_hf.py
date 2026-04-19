"""
Backtest: High-Frequency Funding Capture with Leverage.

Vася's strategy:
  - Filter: only coins with |funding rate| > threshold (0.02%+)
  - Enter: 5 seconds before funding timestamp
  - Direction: LONG if funding negative (longs receive), SHORT if positive
  - Leverage: 10x
  - Exit: immediately after funding credited (~5s hold)
  - P&L: funding_received - price_impact - fees

We simulate using:
  - Funding rate history (Binance Futures, public)
  - 1m candles around funding events for price impact estimation
  - 5s price impact estimated from 1m volatility (vol_1m / sqrt(12))

Usage:
    python scripts/research/backtest_funding_hf.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import ccxt

DATA = ROOT / "data" / "processed"

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "AVAX/USDT:USDT", "SUI/USDT:USDT",
    "ARB/USDT:USDT", "INJ/USDT:USDT", "NEAR/USDT:USDT", "AAVE/USDT:USDT",
    "OP/USDT:USDT", "FIL/USDT:USDT", "ADA/USDT:USDT", "DOT/USDT:USDT",
    "HBAR/USDT:USDT", "FET/USDT:USDT", "TIA/USDT:USDT", "WIF/USDT:USDT",
]

# Binance futures: funding every 8h (00:00, 08:00, 16:00 UTC)
# Some coins have 4h funding (00,04,08,12,16,20 UTC)


def fetch_funding_history(exchange, symbol, months=3):
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_rates, cur = [], since_ms
    while True:
        try:
            rates = exchange.fetch_funding_rate_history(symbol, since=cur, limit=1000)
        except Exception as e:
            break
        if not rates:
            break
        all_rates.extend(rates)
        last_ts = rates[-1]["timestamp"]
        if last_ts <= cur or len(rates) < 100:
            break
        cur = last_ts + 1
        time.sleep(0.2)
    if not all_rates:
        return pd.DataFrame()
    records = [{"ts": pd.to_datetime(r["timestamp"], unit="ms", utc=True),
                "funding_rate": r["fundingRate"], "symbol": symbol} for r in all_rates]
    return pd.DataFrame(records).drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def fetch_1m_candle_at_funding(exchange, symbol, funding_ts_ms):
    """Fetch the 1m candle containing the funding event."""
    since = funding_ts_ms - 2 * 60 * 1000
    try:
        candles = exchange.fetch_ohlcv(symbol, "1m", since=since, limit=5)
    except Exception:
        return None
    if not candles:
        return None
    # Find candle closest to funding
    best = min(candles, key=lambda c: abs(c[0] - funding_ts_ms))
    return {"open": best[1], "high": best[2], "low": best[3], "close": best[4], "volume": best[5]}


def simulate_funding_capture(
    funding_df: pd.DataFrame,
    exchange,
    symbol: str,
    leverage: float = 10.0,
    margin_per_trade: float = 1000.0,
    min_funding_rate: float = 0.0002,  # 0.02%
    fee_per_side: float = 0.0004,      # taker 4 bps
    sample_1m: bool = True,
    max_events: int = 200,
) -> list[dict]:
    """Simulate funding capture for one symbol."""
    trades = []

    # Filter by minimum funding rate
    df = funding_df[funding_df["funding_rate"].abs() >= min_funding_rate].copy()
    if df.empty:
        return []

    df = df.tail(max_events)

    for _, row in df.iterrows():
        fr = row["funding_rate"]
        ts = row["ts"]
        ts_ms = int(ts.timestamp() * 1000)

        notional = margin_per_trade * leverage
        is_long = fr < 0  # longs receive when funding negative

        # Funding received (absolute)
        funding_usd = abs(fr) * notional

        # Estimate price impact during 5s hold
        if sample_1m:
            candle = fetch_1m_candle_at_funding(exchange, symbol, ts_ms)
            time.sleep(0.1)
            if candle:
                # 1m candle range as volatility estimate
                price = candle["close"]
                range_1m = (candle["high"] - candle["low"]) / price
                # 5s volatility estimate: range_1m / sqrt(12)
                # (12 five-second intervals in 1 minute)
                est_5s_impact = range_1m / np.sqrt(12)
                # Random direction for price impact (conservative: always against us)
                price_impact_pct = est_5s_impact
            else:
                price_impact_pct = 0.0005  # default 5 bps
        else:
            price_impact_pct = 0.0005

        price_impact_usd = price_impact_pct * notional

        # Fees: entry + exit (taker)
        fee_usd = 2 * fee_per_side * notional

        # Net P&L
        net_pnl = funding_usd - price_impact_usd - fee_usd

        trades.append({
            "ts": ts,
            "symbol": symbol,
            "funding_rate": fr,
            "funding_rate_abs": abs(fr),
            "is_long": is_long,
            "notional": notional,
            "funding_usd": funding_usd,
            "price_impact_usd": price_impact_usd,
            "fee_usd": fee_usd,
            "net_pnl": net_pnl,
            "net_pnl_pct": net_pnl / margin_per_trade * 100,
        })

    return trades


def main():
    print("=== HF Funding Capture Backtest ===\n")

    exchange = ccxt.binanceusdm({"enableRateLimit": True})

    # 1. Download funding rates
    print("1. Downloading funding rates (3 months)...\n")
    all_funding = {}
    for symbol in SYMBOLS:
        sys.stdout.write(f"  {symbol}...")
        sys.stdout.flush()
        df = fetch_funding_history(exchange, symbol, months=3)
        if not df.empty:
            high = df[df["funding_rate"].abs() >= 0.0002]
            print(f" {len(df)} events, {len(high)} above 2bps filter "
                  f"(max {df['funding_rate'].abs().max()*100:.3f}%)")
            all_funding[symbol] = df
        else:
            print(" FAILED")

    # 2. Simulate across configs
    configs = [
        # (name, min_rate, leverage, margin, fee)
        ("Base: >2bps, 10x, taker", 0.0002, 10, 1000, 0.0004),
        ("High filter: >5bps, 10x", 0.0005, 10, 1000, 0.0004),
        ("Extreme: >10bps, 10x", 0.0010, 10, 1000, 0.0004),
        ("Maker fees: >2bps, 10x", 0.0002, 10, 1000, 0.0002),
        ("20x leverage: >2bps", 0.0002, 20, 1000, 0.0004),
        ("5x conservative: >5bps", 0.0005, 5, 2000, 0.0004),
    ]

    # Sample 1m candles for price impact (only for first config, reuse estimate for others)
    print(f"\n2. Sampling 1m candles for price impact (4 symbols)...\n")
    price_impacts = []
    for symbol in list(all_funding.keys())[:4]:
        df = all_funding[symbol]
        high = df[df["funding_rate"].abs() >= 0.0002].tail(20)
        for _, row in high.iterrows():
            ts_ms = int(row["ts"].timestamp() * 1000)
            candle = fetch_1m_candle_at_funding(exchange, symbol, ts_ms)
            time.sleep(0.1)
            if candle and candle["close"] > 0:
                range_pct = (candle["high"] - candle["low"]) / candle["close"]
                impact_5s = range_pct / np.sqrt(12)
                price_impacts.append(impact_5s)

    if price_impacts:
        avg_impact = np.mean(price_impacts) * 100
        med_impact = np.median(price_impacts) * 100
        p90_impact = np.percentile(price_impacts, 90) * 100
        print(f"  Price impact (5s estimate from 1m candles):")
        print(f"    Mean: {avg_impact:.3f}%")
        print(f"    Median: {med_impact:.3f}%")
        print(f"    P90: {p90_impact:.3f}%")
        default_impact = np.mean(price_impacts)
    else:
        default_impact = 0.0005
        print(f"  No candles fetched, using default {default_impact*100:.3f}%")

    # 3. Run simulations (without API calls, using estimated impact)
    print(f"\n3. Simulation results (3 months):\n")

    for name, min_rate, leverage, margin, fee in configs:
        all_trades = []
        for symbol, fdf in all_funding.items():
            filtered = fdf[fdf["funding_rate"].abs() >= min_rate]
            for _, row in filtered.iterrows():
                fr = row["funding_rate"]
                notional = margin * leverage
                funding_usd = abs(fr) * notional
                impact_usd = default_impact * notional
                fee_usd = 2 * fee * notional
                net = funding_usd - impact_usd - fee_usd

                all_trades.append({
                    "ts": row["ts"],
                    "symbol": symbol,
                    "funding_rate_abs": abs(fr),
                    "funding_usd": funding_usd,
                    "impact_usd": impact_usd,
                    "fee_usd": fee_usd,
                    "net_pnl": net,
                })

        if not all_trades:
            print(f"  {name}: no trades")
            continue

        tdf = pd.DataFrame(all_trades)
        n = len(tdf)
        days = max(1, (tdf["ts"].max() - tdf["ts"].min()).days)
        n_per_day = n / days

        profitable = tdf[tdf["net_pnl"] > 0]
        wr = len(profitable) / n
        total_pnl = tdf["net_pnl"].sum()
        avg_pnl = tdf["net_pnl"].mean()
        daily_pnl = total_pnl / days
        monthly_pnl = daily_pnl * 30
        avg_funding = tdf["funding_usd"].mean()
        avg_impact = tdf["impact_usd"].mean()
        avg_fee = tdf["fee_usd"].mean()

        # Max single loss
        max_loss = tdf["net_pnl"].min()

        print(f"  {name}")
        print(f"    Trades: {n} ({n_per_day:.1f}/day), WR: {wr*100:.0f}%")
        print(f"    Avg: funding ${avg_funding:.2f} - impact ${avg_impact:.2f} - fee ${avg_fee:.2f} = net ${avg_pnl:.2f}")
        print(f"    Daily P&L: ${daily_pnl:.2f}, Monthly: ${monthly_pnl:.0f}")
        print(f"    Total ({days}d): ${total_pnl:.0f}")
        print(f"    Worst trade: ${max_loss:.2f}")
        print(f"    On ${margin} margin = {monthly_pnl/margin*100:.1f}%/month\n")

    # 4. Per-symbol breakdown (base config)
    print(f"4. Per-symbol (>2bps, 10x, taker fees):\n")
    print(f"  {'Symbol':18s} {'Trades':>7} {'Events/d':>8} {'AvgNet':>8} {'Monthly':>9} {'WR':>5}")
    for symbol in sorted(all_funding.keys()):
        fdf = all_funding[symbol]
        filtered = fdf[fdf["funding_rate"].abs() >= 0.0002]
        if filtered.empty:
            continue
        days = max(1, (fdf["ts"].max() - fdf["ts"].min()).days)
        trades_per_day = len(filtered) / days

        nets = []
        for _, row in filtered.iterrows():
            notional = 10000  # 10x on $1k
            funding = abs(row["funding_rate"]) * notional
            impact = default_impact * notional
            fee = 2 * 0.0004 * notional
            nets.append(funding - impact - fee)

        nets = np.array(nets)
        avg_net = nets.mean()
        wr = (nets > 0).mean()
        monthly = avg_net * trades_per_day * 30

        print(f"  {symbol:18s} {len(filtered):>7} {trades_per_day:>7.1f} ${avg_net:>7.2f} ${monthly:>8.0f} {wr*100:>4.0f}%")


if __name__ == "__main__":
    main()
