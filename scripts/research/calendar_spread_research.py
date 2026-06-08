"""
Calendar Spread Research: Quarterly Futures vs Perpetual on Binance.

De Prado approach: spread = Q_price - Perp_price is (should be) stationary.
Test: ADF, half-life, z-score mean-reversion strategy.

Usage:
    python scripts/research/calendar_spread_research.py [--months 6] [--symbol BTC]
"""

import argparse
import asyncio
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller
from scipy import stats

DATA_DIR = Path("data/raw/calendar_spread")
REPORT_DIR = Path("data/reports")


# -- 1. Data Download --------------------------------------------------

async def get_quarterly_symbols(exchange, base: str) -> list[dict]:
    """Find all active quarterly futures for a base asset."""
    await exchange.load_markets()
    contracts = []
    for sym, m in exchange.markets.items():
        if (m.get("expiry")
            and m.get("base") == base
            and m.get("quote") == "USDT"
            and m.get("settle") == "USDT"
            and m.get("linear")):
            contracts.append({
                "symbol": sym,
                "expiry": m["expiry"],
                "expiry_dt": datetime.fromtimestamp(m["expiry"] / 1000, tz=timezone.utc),
            })
    return sorted(contracts, key=lambda x: x["expiry"])


async def fetch_ohlcv_paginated(
    exchange, symbol: str, timeframe: str = "1h",
    since_ms: int | None = None, until_ms: int | None = None,
    limit_per_req: int = 1500,
) -> pd.DataFrame:
    """Download OHLCV with pagination."""
    all_candles = []
    since = since_ms or int((datetime.now(timezone.utc) - timedelta(days=180)).timestamp() * 1000)
    until = until_ms or int(datetime.now(timezone.utc).timestamp() * 1000)

    while since < until:
        try:
            candles = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit_per_req)
        except Exception as e:
            print(f"  Error fetching {symbol} from {since}: {e}")
            break

        if not candles:
            break

        all_candles.extend(candles)
        last_ts = candles[-1][0]
        if last_ts <= since:
            break
        since = last_ts + 1
        await asyncio.sleep(0.1)  # rate limit

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    return df


async def download_spread_data(base: str = "BTC", timeframe: str = "1h", months: int = 6):
    """Download perp + quarterly data, build spread."""
    exchange = ccxt.binanceusdm({"enableRateLimit": True})

    try:
        # Find quarterly contracts
        contracts = await get_quarterly_symbols(exchange, base)
        perp_sym = f"{base}/USDT:USDT"

        print(f"\n{'='*70}")
        print(f"Calendar Spread Research: {base}")
        print(f"{'='*70}")
        print(f"\nPerp: {perp_sym}")
        print(f"Quarterly contracts found: {len(contracts)}")
        for c in contracts:
            print(f"  {c['symbol']:40s} expiry={c['expiry_dt'].strftime('%Y-%m-%d')}")

        if not contracts:
            print("\nNo quarterly contracts found! Only COIN-M may be available.")
            await exchange.close()
            return None

        since_ms = int((datetime.now(timezone.utc) - timedelta(days=months * 30)).timestamp() * 1000)

        # Download perp
        print(f"\nDownloading {perp_sym} {timeframe} ({months}m)...")
        df_perp = await fetch_ohlcv_paginated(exchange, perp_sym, timeframe, since_ms=since_ms)
        print(f"  Got {len(df_perp)} candles")

        # Download each quarterly
        quarterly_data = {}
        for c in contracts:
            sym = c["symbol"]
            print(f"Downloading {sym} {timeframe}...")
            df_q = await fetch_ohlcv_paginated(exchange, sym, timeframe, since_ms=since_ms)
            print(f"  Got {len(df_q)} candles")
            if not df_q.empty:
                quarterly_data[sym] = {"df": df_q, "expiry": c["expiry_dt"]}

        # Save raw data
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df_perp.to_parquet(DATA_DIR / f"{base}_perp_{timeframe}.parquet")
        for sym, d in quarterly_data.items():
            safe_name = sym.replace("/", "_").replace(":", "_")
            d["df"].to_parquet(DATA_DIR / f"{safe_name}_{timeframe}.parquet")

        return {"perp": df_perp, "quarterly": quarterly_data, "base": base}

    finally:
        await exchange.close()


# -- 2. Spread Construction --------------------------------------------

def build_spread(perp_df: pd.DataFrame, q_df: pd.DataFrame, q_expiry: datetime) -> pd.DataFrame:
    """Build spread = quarterly_close - perp_close, normalized to basis points."""
    perp = perp_df[["ts", "close"]].rename(columns={"close": "perp"})
    q = q_df[["ts", "close"]].rename(columns={"close": "quarterly"})

    df = pd.merge(perp, q, on="ts", how="inner")
    df["spread_abs"] = df["quarterly"] - df["perp"]  # absolute spread
    df["spread_pct"] = df["spread_abs"] / df["perp"] * 100  # percentage spread (basis)
    df["days_to_expiry"] = (q_expiry - df["ts"]).dt.total_seconds() / 86400
    df["spread_ann"] = df["spread_pct"] / df["days_to_expiry"].clip(lower=0.1) * 365  # annualized

    # Filter out post-expiry rows
    df = df[df["days_to_expiry"] > 0].copy()
    return df


# -- 3. Statistical Analysis ------------------------------------------

def analyze_spread(df: pd.DataFrame, label: str):
    """ADF test, half-life, distribution stats."""
    spread = df["spread_pct"].dropna()

    print(f"\n{'-'*60}")
    print(f"Analysis: {label}")
    print(f"{'-'*60}")
    print(f"  Period: {df['ts'].iloc[0].strftime('%Y-%m-%d')} -> {df['ts'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  Observations: {len(spread)}")

    # Basic stats
    print(f"\n  Spread (% basis):")
    print(f"    Mean:    {spread.mean():+.4f}%")
    print(f"    Median:  {spread.median():+.4f}%")
    print(f"    Std:     {spread.std():.4f}%")
    print(f"    Min:     {spread.min():+.4f}%")
    print(f"    Max:     {spread.max():+.4f}%")
    print(f"    Skew:    {spread.skew():.2f}")
    print(f"    Kurt:    {spread.kurtosis():.2f}")

    # ADF test for stationarity
    try:
        adf_stat, adf_p, adf_lags, nobs, crit, _ = adfuller(spread, maxlag=48, regression="c")
        print(f"\n  ADF Test (H0: unit root / non-stationary):")
        print(f"    Statistic: {adf_stat:.4f}")
        print(f"    p-value:   {adf_p:.6f}")
        print(f"    Lags used: {adf_lags}")
        for k, v in crit.items():
            marker = " <- reject" if adf_stat < v else ""
            print(f"    {k}: {v:.4f}{marker}")

        stationary = adf_p < 0.05
        print(f"    -> {'STATIONARY OK' if stationary else 'NON-STATIONARY FAIL'}")
    except Exception as e:
        print(f"  ADF failed: {e}")
        stationary = False

    # Half-life of mean reversion (OU process)
    spread_arr = spread.values
    spread_lag = spread_arr[:-1]
    spread_diff = np.diff(spread_arr)
    slope, intercept, r, p, se = stats.linregress(spread_lag, spread_diff)
    if slope < 0:
        half_life = -np.log(2) / slope
        print(f"\n  Mean-Reversion (OU model):")
        print(f"    Slope (theta):    {slope:.6f}")
        print(f"    Half-life:    {half_life:.1f} bars")
        print(f"    R2:           {r**2:.4f}")
        print(f"    -> Reverts to mean in ~{half_life:.0f} hours")
    else:
        half_life = np.inf
        print(f"\n  Mean-Reversion: NONE (slope={slope:.6f} ≥ 0, spread is trending)")

    # Annualized spread stats
    ann = df["spread_ann"].dropna()
    print(f"\n  Annualized basis:")
    print(f"    Mean APR:  {ann.mean():+.1f}%")
    print(f"    Current:   {ann.iloc[-1]:+.1f}%")

    return {
        "stationary": stationary,
        "adf_p": adf_p if stationary else 1.0,
        "half_life": half_life,
        "mean": spread.mean(),
        "std": spread.std(),
        "apr_mean": ann.mean(),
    }


# -- 4. Z-Score Backtest ----------------------------------------------

def backtest_zscore(df: pd.DataFrame, label: str,
                    lookback: int = 168,  # 7 days for 1h
                    entry_z: float = 2.0,
                    exit_z: float = 0.5,
                    stop_z: float = 4.0,
                    fee_pct: float = 0.02,  # 2 bps per leg, 2 legs = 4 bps round trip
                    ) -> dict:
    """
    Mean-reversion on spread z-score.

    Long spread (long quarterly, short perp) when z < -entry_z
    Short spread (short quarterly, long perp) when z > +entry_z
    Exit when |z| < exit_z or |z| > stop_z
    """
    spread = df["spread_pct"].values
    n = len(spread)

    print(f"\n{'-'*60}")
    print(f"Z-Score Backtest: {label}")
    print(f"  Lookback={lookback}h, Entry=±{entry_z}sigma, Exit=±{exit_z}sigma, Stop=±{stop_z}sigma")
    print(f"  Fee={fee_pct*100:.1f}bps per leg (round trip = {fee_pct*2*100:.1f}bps)")
    print(f"{'-'*60}")

    # Calculate rolling z-score
    df = df.copy()
    df["spread_ma"] = df["spread_pct"].rolling(lookback, min_periods=lookback).mean()
    df["spread_std"] = df["spread_pct"].rolling(lookback, min_periods=lookback).std()
    df["z"] = (df["spread_pct"] - df["spread_ma"]) / df["spread_std"]

    position = 0  # +1 = long spread, -1 = short spread
    trades = []
    entry_price = 0.0
    entry_idx = 0

    for i in range(lookback, n):
        z = df["z"].iloc[i]
        spread_val = df["spread_pct"].iloc[i]

        if np.isnan(z):
            continue

        if position == 0:
            # Entry
            if z < -entry_z:
                position = 1  # long spread (expect spread to widen / revert up)
                entry_price = spread_val
                entry_idx = i
            elif z > entry_z:
                position = -1  # short spread (expect spread to narrow / revert down)
                entry_price = spread_val
                entry_idx = i
        else:
            # Exit
            exit_signal = False
            if position == 1 and (z > -exit_z or z > stop_z):
                exit_signal = True
            elif position == -1 and (z < exit_z or z < -stop_z):
                exit_signal = True

            # Force exit 24h before expiry
            if df["days_to_expiry"].iloc[i] < 1:
                exit_signal = True

            if exit_signal:
                pnl = (spread_val - entry_price) * position
                pnl -= fee_pct * 2  # round trip fees on spread
                duration = i - entry_idx
                trades.append({
                    "entry_ts": df["ts"].iloc[entry_idx],
                    "exit_ts": df["ts"].iloc[i],
                    "direction": "long_spread" if position == 1 else "short_spread",
                    "entry_spread": entry_price,
                    "exit_spread": spread_val,
                    "pnl_pct": pnl,
                    "duration_h": duration,
                    "entry_z": df["z"].iloc[entry_idx],
                    "exit_z": z,
                })
                position = 0

    if not trades:
        print("  No trades generated!")
        return {"n_trades": 0}

    tdf = pd.DataFrame(trades)
    winners = tdf[tdf["pnl_pct"] > 0]

    total_pnl = tdf["pnl_pct"].sum()
    win_rate = len(winners) / len(tdf) * 100
    avg_pnl = tdf["pnl_pct"].mean()
    avg_win = winners["pnl_pct"].mean() if len(winners) > 0 else 0
    avg_loss = tdf[tdf["pnl_pct"] <= 0]["pnl_pct"].mean() if len(tdf[tdf["pnl_pct"] <= 0]) > 0 else 0
    avg_dur = tdf["duration_h"].mean()
    sharpe = tdf["pnl_pct"].mean() / tdf["pnl_pct"].std() * np.sqrt(len(tdf)) if tdf["pnl_pct"].std() > 0 else 0

    # Profit factor
    gross_win = winners["pnl_pct"].sum() if len(winners) > 0 else 0
    gross_loss = abs(tdf[tdf["pnl_pct"] <= 0]["pnl_pct"].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else np.inf

    # Max drawdown on cumulative PnL
    cum = tdf["pnl_pct"].cumsum()
    peak = cum.cummax()
    dd = (cum - peak).min()

    print(f"\n  Results:")
    print(f"    Trades:       {len(tdf)}")
    print(f"    Win rate:     {win_rate:.1f}%")
    print(f"    Total PnL:    {total_pnl:+.4f}%")
    print(f"    Avg PnL:      {avg_pnl:+.4f}%")
    print(f"    Avg win:      {avg_win:+.4f}%")
    print(f"    Avg loss:     {avg_loss:+.4f}%")
    print(f"    Profit factor:{pf:.2f}")
    print(f"    Sharpe:       {sharpe:.2f}")
    print(f"    Max DD:       {dd:+.4f}%")
    print(f"    Avg duration: {avg_dur:.0f}h")

    print(f"\n  Direction breakdown:")
    for d in ["long_spread", "short_spread"]:
        sub = tdf[tdf["direction"] == d]
        if len(sub) > 0:
            print(f"    {d:15s}: {len(sub)} trades, "
                  f"WR={len(sub[sub['pnl_pct']>0])/len(sub)*100:.0f}%, "
                  f"PnL={sub['pnl_pct'].sum():+.4f}%")

    # Show latest trades
    print(f"\n  Last 5 trades:")
    for _, t in tdf.tail(5).iterrows():
        print(f"    {t['entry_ts'].strftime('%m-%d %H:%M')} -> {t['exit_ts'].strftime('%m-%d %H:%M')} "
              f"{t['direction']:15s} z={t['entry_z']:+.1f}->{t['exit_z']:+.1f} "
              f"pnl={t['pnl_pct']:+.4f}% ({t['duration_h']:.0f}h)")

    return {
        "n_trades": len(tdf),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "profit_factor": pf,
        "max_dd": dd,
        "avg_duration_h": avg_dur,
        "trades_df": tdf,
    }


# -- 5. Parameter Sensitivity -----------------------------------------

def sensitivity_scan(df: pd.DataFrame, label: str):
    """Quick scan over lookback and entry_z parameters."""
    print(f"\n{'-'*60}")
    print(f"Sensitivity Scan: {label}")
    print(f"{'-'*60}")
    print(f"  {'Lookback':>10} {'Entry_Z':>8} {'Trades':>7} {'WR%':>6} {'PnL%':>8} {'Sharpe':>7} {'PF':>6}")

    best = None
    for lookback in [72, 168, 336]:  # 3d, 7d, 14d
        for entry_z in [1.5, 2.0, 2.5, 3.0]:
            r = backtest_zscore.__wrapped__(df, lookback=lookback, entry_z=entry_z) if hasattr(backtest_zscore, '__wrapped__') else _backtest_silent(df, lookback, entry_z)
            if r["n_trades"] > 0:
                print(f"  {lookback:>10} {entry_z:>8.1f} {r['n_trades']:>7} "
                      f"{r['win_rate']:>5.1f}% {r['total_pnl']:>+7.4f}% "
                      f"{r['sharpe']:>6.2f} {r['profit_factor']:>5.2f}")
                if best is None or r["sharpe"] > best["sharpe"]:
                    best = {**r, "lookback": lookback, "entry_z": entry_z}

    if best:
        print(f"\n  Best: lookback={best['lookback']}h, entry_z={best['entry_z']}, "
              f"Sharpe={best['sharpe']:.2f}, PnL={best['total_pnl']:+.4f}%")
    return best


def _backtest_silent(df, lookback, entry_z, exit_z=0.5, stop_z=4.0, fee_pct=0.02):
    """Silent version of backtest for parameter scan."""
    spread = df["spread_pct"].values
    n = len(spread)

    _df = df.copy()
    _df["spread_ma"] = _df["spread_pct"].rolling(lookback, min_periods=lookback).mean()
    _df["spread_std"] = _df["spread_pct"].rolling(lookback, min_periods=lookback).std()
    _df["z"] = (_df["spread_pct"] - _df["spread_ma"]) / _df["spread_std"]

    position = 0
    trades = []
    entry_price = 0.0
    entry_idx = 0

    for i in range(lookback, n):
        z = _df["z"].iloc[i]
        spread_val = _df["spread_pct"].iloc[i]
        if np.isnan(z):
            continue

        if position == 0:
            if z < -entry_z:
                position, entry_price, entry_idx = 1, spread_val, i
            elif z > entry_z:
                position, entry_price, entry_idx = -1, spread_val, i
        else:
            exit_signal = False
            if position == 1 and (z > -exit_z or z > stop_z):
                exit_signal = True
            elif position == -1 and (z < exit_z or z < -stop_z):
                exit_signal = True
            if _df["days_to_expiry"].iloc[i] < 1:
                exit_signal = True
            if exit_signal:
                pnl = (spread_val - entry_price) * position - fee_pct * 2
                trades.append({"pnl_pct": pnl})
                position = 0

    if not trades:
        return {"n_trades": 0, "win_rate": 0, "total_pnl": 0, "sharpe": 0, "profit_factor": 0}

    pnls = [t["pnl_pct"] for t in trades]
    pnls = np.array(pnls)
    winners = pnls[pnls > 0]
    losers = pnls[pnls <= 0]

    return {
        "n_trades": len(pnls),
        "win_rate": len(winners) / len(pnls) * 100,
        "total_pnl": pnls.sum(),
        "sharpe": pnls.mean() / pnls.std() * np.sqrt(len(pnls)) if pnls.std() > 0 else 0,
        "profit_factor": winners.sum() / abs(losers.sum()) if len(losers) > 0 and losers.sum() != 0 else np.inf,
    }


# -- 6. Main ----------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC", help="Base asset (BTC, ETH)")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--skip-download", action="store_true", help="Use cached data")
    args = parser.parse_args()

    # Download or load
    if args.skip_download:
        print("Loading cached data...")
        perp_path = DATA_DIR / f"{args.symbol}_perp_{args.timeframe}.parquet"
        if not perp_path.exists():
            print(f"No cached data at {perp_path}. Run without --skip-download first.")
            return
        df_perp = pd.read_parquet(perp_path)

        # Find quarterly files
        quarterly_data = {}
        for f in DATA_DIR.glob(f"{args.symbol}_USDT_USDT-*_{args.timeframe}.parquet"):
            df_q = pd.read_parquet(f)
            # Extract expiry from filename
            parts = f.stem.split("-")
            sym_part = f.stem.replace(f"_{args.timeframe}", "")
            quarterly_data[sym_part] = {"df": df_q, "expiry": df_q["ts"].max()}
        data = {"perp": df_perp, "quarterly": quarterly_data, "base": args.symbol}
    else:
        data = await download_spread_data(args.symbol, args.timeframe, args.months)

    if not data or not data["quarterly"]:
        print("\nNo quarterly data available. Try --symbol ETH or check exchange.")
        return

    # Analyze each quarterly contract
    all_results = []
    for sym, qdata in data["quarterly"].items():
        df_spread = build_spread(data["perp"], qdata["df"], qdata["expiry"])
        if len(df_spread) < 200:
            print(f"\nSkipping {sym}: only {len(df_spread)} obs (need 200+)")
            continue

        # Statistical analysis
        stats_result = analyze_spread(df_spread, sym)

        # Backtest with default params
        bt_result = backtest_zscore(df_spread, sym)

        # Sensitivity scan
        if len(df_spread) > 500:
            best = sensitivity_scan(df_spread, sym)

        all_results.append({"symbol": sym, "stats": stats_result, "backtest": bt_result})

    # Summary
    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    for r in all_results:
        s = r["stats"]
        b = r["backtest"]
        status = "STATIONARY" if s["stationary"] else "non-stat"
        print(f"  {r['symbol']:40s} {status:12s} "
              f"HL={s['half_life']:.0f}h "
              f"APR={s['apr_mean']:+.1f}% "
              f"trades={b['n_trades']} "
              f"sharpe={b.get('sharpe', 0):.2f}")

    print(f"\n  Verdict:")
    tradeable = [r for r in all_results if r["stats"]["stationary"] and r["backtest"]["n_trades"] > 5]
    if tradeable:
        print(f"  {len(tradeable)} contract(s) show stationary spread -> mean-reversion viable")
        for r in tradeable:
            print(f"    -> {r['symbol']}: half-life={r['stats']['half_life']:.0f}h, "
                  f"Sharpe={r['backtest'].get('sharpe', 0):.2f}")
    else:
        print("  No contracts with stationary spread found.")
        print("  Options: try ETH, try longer lookback, or spread may be trending in current regime.")


if __name__ == "__main__":
    asyncio.run(main())
