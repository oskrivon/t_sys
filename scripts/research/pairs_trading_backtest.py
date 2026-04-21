"""
Cross-Pair Spread / Pairs Trading backtest.
Hypothesis: ETH/BTC ratio (and other pairs) mean-reverts.
When ratio deviates from moving average, trade the reversion.

Delta-neutral: equal dollar amounts on each leg.
"""
import ccxt
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import time

# --- Config ---
API_KEY = "<BYBIT_API_KEY>"
API_SECRET = "<BYBIT_API_SECRET>"

PAIRS_TO_TEST = [
    ("ETH/USDT:USDT", "BTC/USDT:USDT", "ETH/BTC"),
    ("SOL/USDT:USDT", "ETH/USDT:USDT", "SOL/ETH"),
    ("DOGE/USDT:USDT", "BTC/USDT:USDT", "DOGE/BTC"),
    ("LINK/USDT:USDT", "ETH/USDT:USDT", "LINK/ETH"),
]

LOOKBACK = 48        # rolling window for mean/std (hours)
Z_ENTRY = 2.0        # z-score threshold to enter
Z_EXIT = 0.5         # z-score threshold to exit
FEE_BPS = 11         # one-way fee per leg in bps (taker)
LIMIT = 2000         # candles to fetch per request


def main():
    exchange = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {"defaultType": "swap"},
    })

    since = exchange.parse8601(
        (datetime.now(tz=None) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S")
    )

    def fetch_ohlcv(symbol):
        all_candles = []
        current_since = since
        while True:
            candles = exchange.fetch_ohlcv(symbol, "1h", since=current_since, limit=LIMIT)
            if not candles:
                break
            all_candles.extend(candles)
            if len(candles) < LIMIT:
                break
            current_since = candles[-1][0] + 1
            time.sleep(0.2)
        df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df = df.set_index("ts")
        df = df[~df.index.duplicated(keep="first")]
        return df

    # Collect all unique symbols
    all_symbols = set()
    for s1, s2, _ in PAIRS_TO_TEST:
        all_symbols.add(s1)
        all_symbols.add(s2)

    print("Fetching candle data...")
    data = {}
    for sym in sorted(all_symbols):
        print(f"  {sym}...", end=" ", flush=True)
        data[sym] = fetch_ohlcv(sym)
        print(f"{len(data[sym])} candles")

    def backtest_pair(sym_num, sym_den, label, z_entry=Z_ENTRY, z_exit=Z_EXIT):
        """
        sym_num / sym_den = ratio
        SHORT num + LONG den when ratio high (z > z_entry)
        LONG num + SHORT den when ratio low (z < -z_entry)
        """
        df_num = data[sym_num][["close"]].rename(columns={"close": "num"})
        df_den = data[sym_den][["close"]].rename(columns={"close": "den"})

        merged = df_num.join(df_den, how="inner")
        if len(merged) < LOOKBACK + 10:
            print(f"  {label}: not enough data ({len(merged)} rows)")
            return None

        merged["ratio"] = merged["num"] / merged["den"]
        merged["ratio_mean"] = merged["ratio"].rolling(LOOKBACK).mean()
        merged["ratio_std"] = merged["ratio"].rolling(LOOKBACK).std()
        merged["z"] = (merged["ratio"] - merged["ratio_mean"]) / merged["ratio_std"]

        merged = merged.dropna()

        # Generate trades
        trades = []
        position = 0  # 0=flat, 1=long ratio, -1=short ratio
        entry_z = 0.0
        entry_num_price = 0.0
        entry_den_price = 0.0
        entry_time = None

        for i in range(len(merged)):
            row = merged.iloc[i]
            z = row["z"]

            if position == 0:
                if z > z_entry:
                    # ratio too high -> short ratio (short num, long den)
                    position = -1
                    entry_z = z
                    entry_num_price = row["num"]
                    entry_den_price = row["den"]
                    entry_time = merged.index[i]
                elif z < -z_entry:
                    # ratio too low -> long ratio (long num, short den)
                    position = 1
                    entry_z = z
                    entry_num_price = row["num"]
                    entry_den_price = row["den"]
                    entry_time = merged.index[i]
            else:
                should_exit = False
                if position == -1 and z < z_exit:
                    should_exit = True
                elif position == 1 and z > -z_exit:
                    should_exit = True

                if should_exit:
                    # PnL: delta-neutral, $1 each leg
                    num_ret = (row["num"] - entry_num_price) / entry_num_price
                    den_ret = (row["den"] - entry_den_price) / entry_den_price

                    if position == -1:
                        # short num, long den
                        pnl_pct = (-num_ret + den_ret) / 2
                    else:
                        # long num, short den
                        pnl_pct = (num_ret - den_ret) / 2

                    # Fees: 4 transactions (entry+exit for each leg)
                    # Each taker = FEE_BPS/2 one way (11 bps RT means 5.5 bps each way)
                    fee_cost = 4 * (FEE_BPS / 2) / 10000

                    net_pnl = pnl_pct - fee_cost

                    duration_h = (merged.index[i] - entry_time).total_seconds() / 3600

                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": merged.index[i],
                        "direction": "short_ratio" if position == -1 else "long_ratio",
                        "entry_z": entry_z,
                        "exit_z": z,
                        "pnl_pct": net_pnl * 100,
                        "duration_h": duration_h,
                    })
                    position = 0

        if not trades:
            print(f"  {label}: 0 trades")
            return None

        tdf = pd.DataFrame(trades)

        # Stats
        n_trades = len(tdf)
        wins = (tdf["pnl_pct"] > 0).sum()
        win_rate = wins / n_trades * 100
        total_pnl = tdf["pnl_pct"].sum()
        avg_pnl = tdf["pnl_pct"].mean()
        avg_dur = tdf["duration_h"].mean()

        # Max drawdown on cumulative PnL
        cum_pnl = tdf["pnl_pct"].cumsum()
        peak = cum_pnl.cummax()
        drawdown = cum_pnl - peak
        max_dd = drawdown.min()

        # Sharpe (annualized)
        if tdf["pnl_pct"].std() > 0:
            total_days = (merged.index[-1] - merged.index[0]).total_seconds() / 86400
            trades_per_year = n_trades / total_days * 365 if total_days > 0 else 0
            sharpe = (avg_pnl / tdf["pnl_pct"].std()) * np.sqrt(trades_per_year) if trades_per_year > 0 else 0
        else:
            sharpe = 0

        return {
            "pair": label,
            "n_trades": n_trades,
            "win_rate": f"{win_rate:.1f}%",
            "total_pnl": f"{total_pnl:.2f}%",
            "avg_pnl": f"{avg_pnl:.3f}%",
            "max_dd": f"{max_dd:.2f}%",
            "sharpe": f"{sharpe:.2f}",
            "avg_dur_h": f"{avg_dur:.1f}",
            "best_trade": f"{tdf['pnl_pct'].max():.3f}%",
            "worst_trade": f"{tdf['pnl_pct'].min():.3f}%",
        }

    # --- Run backtests ---
    print(f"\n{'='*70}")
    print("PAIRS TRADING BACKTEST - Mean-Reversion on Price Ratios")
    print(f"Window: {LOOKBACK}h | Entry Z: {Z_ENTRY} | Exit Z: {Z_EXIT} | Fee: {FEE_BPS}bps RT/leg")
    print(f"{'='*70}\n")

    results = []
    for sym_num, sym_den, label in PAIRS_TO_TEST:
        print(f"Testing {label}...")
        r = backtest_pair(sym_num, sym_den, label)
        if r:
            results.append(r)

    print(f"\n{'='*70}")
    print("RESULTS SUMMARY")
    print(f"{'='*70}")

    if results:
        rdf = pd.DataFrame(results)
        print(rdf.to_string(index=False))
    else:
        print("No trades generated for any pair.")

    # --- Sensitivity analysis for ETH/BTC ---
    print(f"\n{'='*70}")
    print("SENSITIVITY: ETH/BTC with different Z-entry thresholds")
    print(f"{'='*70}")

    for z_entry in [1.5, 2.0, 2.5, 3.0]:
        r = backtest_pair("ETH/USDT:USDT", "BTC/USDT:USDT", f"ETH/BTC z={z_entry}",
                          z_entry=z_entry, z_exit=0.5)
        if r:
            print(f"  z={z_entry}: trades={r['n_trades']:>3}, WR={r['win_rate']:>6}, "
                  f"PnL={r['total_pnl']:>8}, avg={r['avg_pnl']:>8}, DD={r['max_dd']:>8}, "
                  f"Sharpe={r['sharpe']:>6}")
        else:
            print(f"  z={z_entry}: no trades")

    # --- Conclusion ---
    print(f"\n{'='*70}")
    print("CONCLUSION")
    print(f"{'='*70}")
    if results:
        best = max(results, key=lambda x: float(x["total_pnl"].replace("%", "")))
        worst = min(results, key=lambda x: float(x["total_pnl"].replace("%", "")))
        total_all = sum(float(r["total_pnl"].replace("%", "")) for r in results)
        print(f"Best pair:  {best['pair']} ({best['total_pnl']} total, {best['sharpe']} Sharpe)")
        print(f"Worst pair: {worst['pair']} ({worst['total_pnl']} total)")
        print(f"Combined PnL across all pairs: {total_all:.2f}%")

        avg_sharpe = np.mean([float(r["sharpe"]) for r in results])
        if avg_sharpe > 1.0 and total_all > 0:
            print("VERDICT: GREEN - pairs trading shows promise after fees")
        elif avg_sharpe > 0.5 or total_all > 0:
            print("VERDICT: YELLOW - marginal edge, needs optimization")
        else:
            print("VERDICT: RED - insufficient edge after fees")
    else:
        print("VERDICT: RED - no trades generated")


if __name__ == "__main__":
    main()
