"""
Funding Rate Scalp Research: enter before funding, exit after.

Strategy claim: enter 2-3 seconds before funding payment, exit in first milliseconds.
Test: is this viable? What are the actual mechanics and edge?

Analysis:
  1. Download funding rate history (Binance Futures)
  2. Download 1-minute candles around funding times
  3. Simulate: long if funding negative (shorts pay longs), short if positive
  4. Measure: actual P&L after fees and slippage

Usage:
    python scripts/research/funding_scalp_research.py
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

# Top futures pairs
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "AVAX/USDT:USDT", "SUI/USDT:USDT",
    "ARB/USDT:USDT", "PEPE/USDT:USDT", "INJ/USDT:USDT", "NEAR/USDT:USDT",
]

# Binance funding happens every 8h: 00:00, 08:00, 16:00 UTC


def fetch_funding_history(exchange, symbol: str, months: int = 6) -> pd.DataFrame:
    """Fetch funding rate history from Binance Futures."""
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_rates = []
    cur = since_ms

    while True:
        try:
            # Binance futures funding rate endpoint
            rates = exchange.fetch_funding_rate_history(symbol, since=cur, limit=1000)
        except Exception as e:
            print(f"  {symbol} funding error: {e}")
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

    records = []
    for r in all_rates:
        records.append({
            "ts": pd.to_datetime(r["timestamp"], unit="ms", utc=True),
            "funding_rate": r["fundingRate"],
            "symbol": symbol,
        })

    return pd.DataFrame(records).drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def fetch_1m_candles_around_funding(exchange, symbol: str, funding_ts_ms: int) -> pd.DataFrame:
    """Fetch 1-minute candles around a funding event (+/- 5 minutes)."""
    since = funding_ts_ms - 5 * 60 * 1000  # 5 min before
    try:
        candles = exchange.fetch_ohlcv(symbol, "1m", since=since, limit=15)
    except Exception:
        return pd.DataFrame()

    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def analyze_funding_scalp(funding_df: pd.DataFrame, symbol: str, exchange) -> dict:
    """Analyze funding scalp P&L for one symbol.

    Strategy: if funding > 0, go SHORT just before (shorts receive payment).
              if funding < 0, go LONG just before (longs receive payment).
    Hold: 1 minute (enter at T-1min, exit at T+0 or T+1min).
    """
    if funding_df.empty:
        return {}

    results = []
    n_checked = 0
    n_skipped = 0

    # Sample up to 100 funding events
    sample = funding_df.tail(300)  # last 300 events (~100 days)

    for _, row in sample.iterrows():
        funding_rate = row["funding_rate"]
        funding_ts = row["ts"]

        # Skip tiny funding rates (not worth the fees)
        if abs(funding_rate) < 0.0001:  # < 0.01%
            n_skipped += 1
            continue

        n_checked += 1

        # Simulate: we don't have tick data, but we can analyze 1m candles
        # The key question is: does price move AGAINST the funding payer?

        # Direction: go with the receiver
        is_long = funding_rate < 0  # negative = longs receive

        # Simulate P&L components:
        # 1. Funding payment received (the edge)
        funding_pnl = abs(funding_rate)  # what we receive

        # 2. Price impact: does price move during funding?
        #    We can't fetch tick data easily, so estimate from known patterns
        #    Research shows price typically moves 0-5 bps around funding

        # 3. Fees: maker 0.02%, taker 0.04% on Binance futures
        #    Entry taker + exit taker = 0.08% round trip
        fee_pct = 0.0008  # 0.08% taker-taker

        # 4. Slippage: ~1-2 bps for large caps
        slippage_pct = 0.0002  # 2 bps total

        # Net P&L per trade
        net_pnl = funding_pnl - fee_pct - slippage_pct

        results.append({
            "ts": funding_ts,
            "funding_rate": funding_rate,
            "funding_pnl": funding_pnl,
            "fee_pct": fee_pct,
            "slippage_pct": slippage_pct,
            "net_pnl": net_pnl,
            "is_profitable": net_pnl > 0,
        })

    if not results:
        return {"symbol": symbol, "trades": 0}

    rdf = pd.DataFrame(results)
    profitable = rdf[rdf["is_profitable"]]
    unprofitable = rdf[~rdf["is_profitable"]]

    return {
        "symbol": symbol,
        "trades": len(rdf),
        "skipped_tiny": n_skipped,
        "profitable_count": len(profitable),
        "profitable_pct": len(profitable) / len(rdf) * 100,
        "avg_funding_rate": rdf["funding_rate"].abs().mean(),
        "avg_funding_pnl": rdf["funding_pnl"].mean() * 100,
        "avg_fee": rdf["fee_pct"].mean() * 100,
        "avg_net_pnl": rdf["net_pnl"].mean() * 100,
        "total_net_pnl": rdf["net_pnl"].sum() * 100,
        "max_funding": rdf["funding_rate"].abs().max(),
        "pct_above_fee": (rdf["funding_pnl"] > fee_pct).mean() * 100,
    }


def analyze_price_behavior_around_funding(exchange, symbol, funding_df, n_samples=30):
    """Check what happens to price in the minute around funding."""
    if funding_df.empty or len(funding_df) < n_samples:
        return {}

    # Sample recent funding events
    sample = funding_df.tail(n_samples * 2).sample(min(n_samples, len(funding_df)), random_state=42)

    moves_before = []  # price move in 1 min BEFORE funding
    moves_after = []   # price move in 1 min AFTER funding
    moves_aligned = [] # move in direction of "smart" side

    for _, row in sample.iterrows():
        ts_ms = int(row["ts"].timestamp() * 1000)
        candles = fetch_1m_candles_around_funding(exchange, symbol, ts_ms)
        if candles.empty or len(candles) < 10:
            continue

        # Find the candle closest to funding time
        funding_ts = row["ts"]
        candles["diff"] = (candles["ts"] - funding_ts).abs()
        closest_idx = candles["diff"].idxmin()

        if closest_idx < 1 or closest_idx >= len(candles) - 1:
            continue

        # Price move before (T-1 to T)
        before = (candles["close"].iloc[closest_idx] - candles["close"].iloc[closest_idx - 1]) / candles["close"].iloc[closest_idx - 1]
        moves_before.append(before * 100)

        # Price move after (T to T+1)
        after = (candles["close"].iloc[closest_idx + 1] - candles["close"].iloc[closest_idx]) / candles["close"].iloc[closest_idx]
        moves_after.append(after * 100)

        # "Smart" side: if funding > 0, shorts are smart (they receive)
        # So smart side move = short move = negative price move
        if row["funding_rate"] > 0:
            aligned = -before  # short before = want price down
        else:
            aligned = before   # long before = want price up
        moves_aligned.append(aligned)

        time.sleep(0.15)

    if not moves_before:
        return {}

    return {
        "symbol": symbol,
        "n_samples": len(moves_before),
        "avg_move_before_bps": np.mean(moves_before) * 100,
        "avg_move_after_bps": np.mean(moves_after) * 100,
        "avg_aligned_move_bps": np.mean(moves_aligned) * 100,
        "std_move_bps": np.std(moves_before) * 100,
        "pct_favorable_before": sum(1 for m in moves_aligned if m > 0) / len(moves_aligned) * 100,
    }


def main():
    print("=== Funding Rate Scalp Research ===\n")

    # Use Binance Futures
    exchange = ccxt.binanceusdm({"enableRateLimit": True})

    # 1. Download funding rate history
    print("1. Downloading funding rate history (6 months)...\n")
    all_funding = {}
    for symbol in SYMBOLS:
        sys.stdout.write(f"  {symbol}...")
        sys.stdout.flush()
        df = fetch_funding_history(exchange, symbol, months=6)
        if not df.empty:
            all_funding[symbol] = df
            avg = df["funding_rate"].mean() * 100
            extreme = df["funding_rate"].abs().max() * 100
            print(f" {len(df)} events, avg {avg:+.4f}%, max |{extreme:.4f}%|")
        else:
            print(" FAILED")

    # 2. Theoretical P&L analysis
    print(f"\n2. Funding scalp P&L analysis (taker fees 0.08%, slippage 2bps)...\n")
    print(f"  {'Symbol':14s} {'Trades':>7} {'Profitable':>11} {'AvgFunding':>11} {'AvgFee':>8} {'AvgNet':>8} {'TotalNet':>9} {'Above fee':>10}")

    all_results = []
    for symbol, fdf in all_funding.items():
        result = analyze_funding_scalp(fdf, symbol, exchange)
        if result and result.get("trades", 0) > 0:
            all_results.append(result)
            r = result
            print(f"  {r['symbol']:14s} {r['trades']:>7} {r['profitable_pct']:>9.0f}% "
                  f"{r['avg_funding_pnl']:>10.4f}% {r['avg_fee']:>7.4f}% "
                  f"{r['avg_net_pnl']:>+7.4f}% {r['total_net_pnl']:>+8.3f}% "
                  f"{r['pct_above_fee']:>8.0f}%")

    # 3. Summary
    if all_results:
        avg_net = np.mean([r["avg_net_pnl"] for r in all_results])
        avg_profitable = np.mean([r["profitable_pct"] for r in all_results])
        avg_above_fee = np.mean([r["pct_above_fee"] for r in all_results])

        print(f"\n  SUMMARY:")
        print(f"  Average net P&L per trade: {avg_net:+.4f}%")
        print(f"  Average profitable %: {avg_profitable:.0f}%")
        print(f"  Avg funding events above fee threshold: {avg_above_fee:.0f}%")
        print(f"  Fee threshold: 0.08% (Binance taker-taker)")

        # Annual estimate
        trades_per_day = 3  # 3 funding per day per symbol
        symbols = len(all_results)
        trades_per_month = trades_per_day * 30 * symbols
        # Only take trades above fee threshold
        effective_trades = trades_per_month * avg_above_fee / 100
        if avg_net > 0:
            monthly = effective_trades * avg_net / 100 * 100  # on 100% capital
            annual = (1 + monthly / 100) ** 12 - 1
            print(f"\n  If profitable:")
            print(f"  Trades/month: {effective_trades:.0f} (above fee threshold)")
            print(f"  Monthly: {monthly:+.2f}%")
            print(f"  Annual: {annual*100:+.1f}%")
        else:
            print(f"\n  Strategy is NEGATIVE after fees.")

    # 4. Check price behavior around funding (sample)
    print(f"\n3. Price behavior around funding (1m candles, sampling)...\n")
    print(f"  {'Symbol':14s} {'N':>4} {'MoveBeforeBps':>14} {'MoveAfterBps':>13} {'AlignedBps':>11} {'%Favorable':>11}")

    for symbol in SYMBOLS[:4]:  # Sample 4 symbols to save API calls
        fdf = all_funding.get(symbol)
        if fdf is None:
            continue
        result = analyze_price_behavior_around_funding(exchange, symbol, fdf, n_samples=20)
        if result:
            r = result
            print(f"  {r['symbol']:14s} {r['n_samples']:>4} "
                  f"{r['avg_move_before_bps']:>+13.2f} {r['avg_move_after_bps']:>+12.2f} "
                  f"{r['avg_aligned_move_bps']:>+10.2f} {r['pct_favorable_before']:>9.0f}%")

    # 5. Verdict
    print(f"\n{'='*65}")
    print(f"  VERDICT")
    print(f"{'='*65}")

    if all_results:
        avg_funding = np.mean([r["avg_funding_pnl"] for r in all_results])
        fee = 0.08
        slippage = 0.02

        print(f"  Average funding rate: {avg_funding:.4f}%")
        print(f"  Fee (taker-taker): {fee:.2f}%")
        print(f"  Slippage estimate: {slippage:.2f}%")
        print(f"  Net per trade: {avg_funding - fee - slippage:+.4f}%")

        if avg_funding > fee + slippage:
            print(f"\n  THEORETICALLY PROFITABLE on average.")
            print(f"  But: need maker fees (0.02%) to be viable -> maker-maker = 0.04%")
            print(f"  With maker fees: net = {avg_funding - 0.04 - slippage:+.4f}%")
        else:
            print(f"\n  NOT PROFITABLE with taker fees.")
            print(f"  With maker fees (0.04%): net = {avg_funding - 0.04 - slippage:+.4f}%")
            print(f"  Only profitable when |funding| > {fee + slippage:.2f}% = {(fee+slippage)*100:.0f} bps")
            above = np.mean([r["pct_above_fee"] for r in all_results])
            print(f"  That happens {above:.0f}% of the time")


if __name__ == "__main__":
    main()
