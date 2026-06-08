"""
Cross-Asset Spread: BTC/ETH ratio trading.

Not arbitrage — statistical arbitrage on cointegration/mean-reversion
of the BTC/ETH price ratio.

Hypothesis: BTC and ETH are cointegrated in certain regimes.
When ratio deviates from equilibrium, it mean-reverts.

Key difference from basis trades:
- Single exchange, single instrument type (perps)
- High liquidity on both legs
- No funding asymmetry (both legs are perps)
- Edge comes from ratio mean-reversion, not structural convergence

Usage:
    python scripts/research/cross_asset_spread.py [--months 12]
    python scripts/research/cross_asset_spread.py --pairs BTC/ETH,SOL/ETH
"""

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from itertools import combinations

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint
from scipy import stats as sp_stats

DATA_DIR = Path("data/raw/cross_asset")


# -- 1. Download ----------------------------------------------------------

async def download_pair(base_a: str, base_b: str, months: int = 12, timeframe: str = "1h"):
    """Download OHLCV for two assets."""
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=months * 30)).timestamp() * 1000)

    async def fetch_all(sym):
        all_c = []
        s = since_ms
        until = int(datetime.now(timezone.utc).timestamp() * 1000)
        while s < until:
            try:
                c = await exchange.fetch_ohlcv(sym, timeframe, since=s, limit=1500)
            except Exception as e:
                print(f"  Error {sym}: {e}")
                break
            if not c:
                break
            all_c.extend(c)
            last = c[-1][0]
            if last <= s:
                break
            s = last + 1
            await asyncio.sleep(0.05)
        df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

    sym_a = f"{base_a}/USDT:USDT"
    sym_b = f"{base_b}/USDT:USDT"

    print(f"Downloading {sym_a} {timeframe} ({months}m)...")
    df_a = await fetch_all(sym_a)
    print(f"  Got {len(df_a)} candles")

    print(f"Downloading {sym_b} {timeframe} ({months}m)...")
    df_b = await fetch_all(sym_b)
    print(f"  Got {len(df_b)} candles")

    await exchange.close()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df_a.to_parquet(DATA_DIR / f"{base_a}_{timeframe}.parquet")
    df_b.to_parquet(DATA_DIR / f"{base_b}_{timeframe}.parquet")

    return df_a, df_b


def load_cached(base: str, tf: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{base}_{tf}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    return None


# -- 2. Ratio Construction -----------------------------------------------

def build_ratio(df_a: pd.DataFrame, df_b: pd.DataFrame,
                name_a: str, name_b: str) -> pd.DataFrame:
    """Build price ratio and log ratio."""
    a = df_a[["ts", "close", "volume"]].rename(
        columns={"close": f"{name_a}", "volume": f"{name_a}_vol"}
    )
    b = df_b[["ts", "close", "volume"]].rename(
        columns={"close": f"{name_b}", "volume": f"{name_b}_vol"}
    )
    df = pd.merge(a, b, on="ts", how="inner")
    df["ratio"] = df[name_a] / df[name_b]
    df["log_ratio"] = np.log(df["ratio"])
    df["log_a"] = np.log(df[name_a])
    df["log_b"] = np.log(df[name_b])
    return df


# -- 3. Cointegration Analysis -------------------------------------------

def analyze_pair(df: pd.DataFrame, name_a: str, name_b: str):
    """Full cointegration and mean-reversion analysis."""
    ratio = df["ratio"]
    log_ratio = df["log_ratio"]

    print(f"\n{'='*70}")
    print(f"Cross-Asset Analysis: {name_a}/{name_b}")
    print(f"{'='*70}")
    print(f"  Period: {df['ts'].iloc[0].strftime('%Y-%m-%d')} -> {df['ts'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  Observations: {len(df):,}")

    # Ratio stats
    print(f"\n  Ratio ({name_a}/{name_b}):")
    print(f"    Mean:   {ratio.mean():.2f}")
    print(f"    Std:    {ratio.std():.2f}")
    print(f"    CV:     {ratio.std()/ratio.mean()*100:.1f}%")
    print(f"    Min:    {ratio.min():.2f}")
    print(f"    Max:    {ratio.max():.2f}")
    print(f"    Current:{ratio.iloc[-1]:.2f}")

    # Correlation
    corr = df[name_a].pct_change().corr(df[name_b].pct_change())
    print(f"    Returns correlation: {corr:.4f}")

    # ADF on ratio
    print(f"\n  ADF Test on ratio:")
    adf_stat, adf_p, lags, nobs, crit, _ = adfuller(ratio.dropna(), maxlag=48)
    print(f"    Statistic: {adf_stat:.4f}, p-value: {adf_p:.6f}, lags: {lags}")
    ratio_stationary = adf_p < 0.05
    print(f"    -> {'STATIONARY' if ratio_stationary else 'NON-STATIONARY'}")

    # ADF on log ratio
    print(f"\n  ADF Test on log(ratio):")
    adf_stat2, adf_p2, lags2, _, _, _ = adfuller(log_ratio.dropna(), maxlag=48)
    print(f"    Statistic: {adf_stat2:.4f}, p-value: {adf_p2:.6f}, lags: {lags2}")
    log_ratio_stationary = adf_p2 < 0.05
    print(f"    -> {'STATIONARY' if log_ratio_stationary else 'NON-STATIONARY'}")

    # Engle-Granger cointegration test
    print(f"\n  Engle-Granger Cointegration Test:")
    try:
        coint_stat, coint_p, coint_crit = coint(
            df[f"log_a"].dropna(), df[f"log_b"].dropna()
        )
        print(f"    Statistic: {coint_stat:.4f}, p-value: {coint_p:.6f}")
        print(f"    Critical: 1%={coint_crit[0]:.3f}, 5%={coint_crit[1]:.3f}, 10%={coint_crit[2]:.3f}")
        cointegrated = coint_p < 0.05
        print(f"    -> {'COINTEGRATED' if cointegrated else 'NOT COINTEGRATED'}")
    except Exception as e:
        print(f"    Error: {e}")
        cointegrated = False

    # Half-life of mean reversion on log ratio
    lr = log_ratio.values
    lr_lag = lr[:-1]
    lr_diff = np.diff(lr)
    slope, intercept, r, p, se = sp_stats.linregress(lr_lag, lr_diff)
    if slope < 0:
        half_life = -np.log(2) / slope
        print(f"\n  Mean-Reversion (OU on log ratio):")
        print(f"    Slope: {slope:.6f}")
        print(f"    Half-life: {half_life:.1f} hours ({half_life/24:.1f} days)")
        print(f"    R2: {r**2:.4f}")
    else:
        half_life = np.inf
        print(f"\n  Mean-Reversion: NONE (trending, slope={slope:.6f})")

    # Rolling regime analysis: is cointegration stable?
    print(f"\n  Rolling cointegration (90-day windows):")
    window = 90 * 24  # hours
    step = 30 * 24
    results = []
    for start in range(0, len(df) - window, step):
        end = start + window
        sub = df.iloc[start:end]
        try:
            _, p_val, _ = coint(sub["log_a"].values, sub["log_b"].values)
            sub_ratio = sub["log_ratio"]
            sub_lr = sub_ratio.values
            sl, _, _, _, _ = sp_stats.linregress(sub_lr[:-1], np.diff(sub_lr))
            hl = -np.log(2) / sl if sl < 0 else np.inf
            results.append({
                "start": sub["ts"].iloc[0].strftime("%Y-%m-%d"),
                "coint_p": p_val,
                "half_life": hl,
                "cointegrated": p_val < 0.05,
            })
        except Exception:
            pass

    if results:
        rdf = pd.DataFrame(results)
        coint_pct = rdf["cointegrated"].mean() * 100
        print(f"  {'Window':>12} {'Coint_p':>8} {'HL(h)':>8} {'Coint?':>6}")
        for _, r in rdf.iterrows():
            hl_str = f"{r['half_life']:.0f}" if r['half_life'] < 10000 else "inf"
            print(f"  {r['start']:>12} {r['coint_p']:>8.4f} {hl_str:>8} "
                  f"{'YES' if r['cointegrated'] else 'no':>6}")
        print(f"\n  Cointegrated in {coint_pct:.0f}% of windows")
    else:
        coint_pct = 0

    return {
        "ratio_stationary": ratio_stationary,
        "log_ratio_stationary": log_ratio_stationary,
        "cointegrated": cointegrated,
        "half_life": half_life,
        "correlation": corr,
        "coint_pct": coint_pct,
    }


# -- 4. Backtest ----------------------------------------------------------

def backtest_ratio(df: pd.DataFrame, name_a: str, name_b: str,
                   lookback: int = 168,     # 7 days
                   entry_z: float = 2.0,
                   exit_z: float = 0.5,
                   stop_z: float = 4.0,
                   fee_pct: float = 0.05,   # 5 bps per leg, taker
                   max_hold: int = 168,     # 7 days max
                   ) -> dict:
    """
    Z-score mean-reversion on log ratio.

    Long ratio (long A, short B) when z < -entry_z
    Short ratio (short A, long B) when z > +entry_z
    """
    log_ratio = df["log_ratio"].values
    n = len(log_ratio)
    rt_fee = fee_pct * 2 * 2 / 100  # 2 legs * 2 (open+close), as fraction

    # Rolling z-score
    _df = df.copy()
    _df["lr_ma"] = _df["log_ratio"].rolling(lookback, min_periods=lookback).mean()
    _df["lr_std"] = _df["log_ratio"].rolling(lookback, min_periods=lookback).std()
    _df["z"] = (_df["log_ratio"] - _df["lr_ma"]) / _df["lr_std"]

    position = 0
    trades = []
    entry_lr = 0.0
    entry_idx = 0
    entry_price_a = 0.0
    entry_price_b = 0.0

    for i in range(lookback, n):
        z = _df["z"].iloc[i]
        lr = _df["log_ratio"].iloc[i]
        price_a = _df[name_a].iloc[i]
        price_b = _df[name_b].iloc[i]

        if np.isnan(z):
            continue

        if position == 0:
            if z < -entry_z:
                position = 1  # long ratio (long A, short B)
                entry_lr = lr
                entry_idx = i
                entry_price_a = price_a
                entry_price_b = price_b
            elif z > entry_z:
                position = -1  # short ratio (short A, long B)
                entry_lr = lr
                entry_idx = i
                entry_price_a = price_a
                entry_price_b = price_b
        else:
            exit_signal = False
            exit_reason = ""

            if position == 1 and z >= -exit_z:
                exit_signal, exit_reason = True, "target"
            elif position == -1 and z <= exit_z:
                exit_signal, exit_reason = True, "target"
            elif abs(z) > stop_z:
                exit_signal, exit_reason = True, "stop"
            elif i - entry_idx > max_hold:
                exit_signal, exit_reason = True, "timeout"

            if exit_signal:
                # PnL: delta(log_ratio) * position
                # In practice: PnL_A + PnL_B
                ret_a = (price_a - entry_price_a) / entry_price_a
                ret_b = (price_b - entry_price_b) / entry_price_b

                if position == 1:  # long A, short B
                    pnl = ret_a - ret_b
                else:  # short A, long B
                    pnl = ret_b - ret_a

                pnl -= rt_fee  # fees

                # Funding: both legs are perps, funding roughly cancels
                # (long pays, short receives, net ~0 if funding similar)
                # But funding can differ between BTC and ETH
                # We'll add a small drag: 1 bps/day net
                days = (i - entry_idx) / 24
                funding_drag = days * 0.0001  # 1 bps/day
                pnl -= funding_drag

                trades.append({
                    "entry_ts": _df["ts"].iloc[entry_idx],
                    "exit_ts": _df["ts"].iloc[i],
                    "direction": "long_ratio" if position == 1 else "short_ratio",
                    "entry_z": _df["z"].iloc[entry_idx],
                    "exit_z": z,
                    "pnl": pnl * 100,  # as percentage
                    "pnl_a": ret_a * 100,
                    "pnl_b": ret_b * 100,
                    "duration_h": i - entry_idx,
                    "exit_reason": exit_reason,
                })
                position = 0

    if not trades:
        print(f"  No trades for {name_a}/{name_b}!")
        return {"n_trades": 0}

    tdf = pd.DataFrame(trades)
    return _report(tdf, name_a, name_b, lookback, entry_z, fee_pct)


def _report(tdf, name_a, name_b, lookback, entry_z, fee_pct):
    """Print backtest report."""
    print(f"\n{'-'*70}")
    print(f"Backtest: {name_a}/{name_b} ratio")
    print(f"  Lookback={lookback}h, Entry=+/-{entry_z}sigma, Fee={fee_pct*100:.0f}bps/leg")
    print(f"{'-'*70}")

    winners = tdf[tdf["pnl"] > 0]
    losers = tdf[tdf["pnl"] <= 0]

    total = tdf["pnl"].sum()
    wr = len(winners) / len(tdf) * 100
    avg = tdf["pnl"].mean()
    avg_win = winners["pnl"].mean() if len(winners) > 0 else 0
    avg_loss = losers["pnl"].mean() if len(losers) > 0 else 0
    gw = winners["pnl"].sum() if len(winners) > 0 else 0
    gl = abs(losers["pnl"].sum()) if len(losers) > 0 else 0
    pf = gw / gl if gl > 0 else float("inf")
    sharpe = avg / tdf["pnl"].std() * np.sqrt(len(tdf)) if tdf["pnl"].std() > 0 else 0

    cum = tdf["pnl"].cumsum()
    dd = (cum - cum.cummax()).min()

    print(f"\n  Results:")
    print(f"    Trades:       {len(tdf)}")
    print(f"    Win rate:     {wr:.1f}%")
    print(f"    Total PnL:    {total:+.2f}%")
    print(f"    Avg PnL:      {avg:+.3f}%")
    print(f"    Avg win:      {avg_win:+.3f}%")
    print(f"    Avg loss:     {avg_loss:+.3f}%")
    print(f"    Profit factor:{pf:.2f}")
    print(f"    Sharpe:       {sharpe:.2f}")
    print(f"    Max DD:       {dd:+.2f}%")
    print(f"    Avg duration: {tdf['duration_h'].mean():.0f}h")

    # By direction
    print(f"\n  Direction:")
    for d in ["long_ratio", "short_ratio"]:
        sub = tdf[tdf["direction"] == d]
        if len(sub) == 0:
            continue
        print(f"    {d:15s}: {len(sub)} trades, "
              f"WR={len(sub[sub['pnl']>0])/len(sub)*100:.0f}%, "
              f"PnL={sub['pnl'].sum():+.2f}%")

    # By exit reason
    print(f"\n  Exit reason:")
    for reason, group in tdf.groupby("exit_reason"):
        print(f"    {reason:10s}: {len(group)} trades, "
              f"WR={len(group[group['pnl']>0])/len(group)*100:.0f}%, "
              f"PnL={group['pnl'].sum():+.2f}%")

    # Last trades
    print(f"\n  Last 5 trades:")
    for _, t in tdf.tail(5).iterrows():
        print(f"    {t['entry_ts'].strftime('%m-%d %H:%M')} -> "
              f"{t['exit_ts'].strftime('%m-%d %H:%M')} "
              f"{t['direction']:15s} z={t['entry_z']:+.1f}->{t['exit_z']:+.1f} "
              f"pnl={t['pnl']:+.3f}% ({t['duration_h']:.0f}h) [{t['exit_reason']}]")

    return {
        "n_trades": len(tdf),
        "total_pnl": total,
        "win_rate": wr,
        "sharpe": sharpe,
        "profit_factor": pf,
        "max_dd": dd,
        "avg_duration": tdf["duration_h"].mean(),
        "trades_df": tdf,
    }


# -- 5. Parameter Scan ---------------------------------------------------

def scan_params(df, name_a, name_b):
    """Scan lookback and entry_z."""
    print(f"\n{'='*70}")
    print(f"Parameter Scan: {name_a}/{name_b}")
    print(f"{'='*70}")
    print(f"  {'LB':>5} {'EntZ':>5} {'MaxH':>5} {'Trades':>6} {'WR%':>5} "
          f"{'PnL%':>8} {'Sharpe':>7} {'PF':>6} {'AvgH':>5}")

    log_ratio = df["log_ratio"].values
    n = len(log_ratio)
    best = None

    for lookback in [72, 168, 336, 720]:
        for entry_z in [1.5, 2.0, 2.5, 3.0]:
            for max_hold in [168, 336]:
                r = _bt_silent(df, name_a, name_b, lookback, entry_z, 0.5, 4.0, 0.05, max_hold)
                if r["n"] < 5:
                    continue
                print(f"  {lookback:>5} {entry_z:>5.1f} {max_hold:>5} "
                      f"{r['n']:>6} {r['wr']:>4.0f}% {r['pnl']:>+7.2f}% "
                      f"{r['sharpe']:>6.2f} {r['pf']:>5.2f} {r['avg_h']:>4.0f}")
                if best is None or r["sharpe"] > best["sharpe"]:
                    best = {**r, "lookback": lookback, "entry_z": entry_z, "max_hold": max_hold}

    if best:
        print(f"\n  Best: LB={best['lookback']}, EntZ={best['entry_z']}, "
              f"MaxH={best['max_hold']}, Sharpe={best['sharpe']:.2f}, "
              f"PnL={best['pnl']:+.2f}%")
    return best


def _bt_silent(df, name_a, name_b, lookback, entry_z, exit_z, stop_z, fee_pct, max_hold):
    """Silent backtest for scanning."""
    _df = df.copy()
    _df["lr_ma"] = _df["log_ratio"].rolling(lookback, min_periods=lookback).mean()
    _df["lr_std"] = _df["log_ratio"].rolling(lookback, min_periods=lookback).std()
    _df["z"] = (_df["log_ratio"] - _df["lr_ma"]) / _df["lr_std"]

    rt_fee = fee_pct * 2 * 2 / 100
    n = len(_df)
    pos = 0
    trades = []
    e_lr = e_pa = e_pb = 0.0
    ei = 0

    for i in range(lookback, n):
        z = _df["z"].iloc[i]
        pa = _df[name_a].iloc[i]
        pb = _df[name_b].iloc[i]
        if np.isnan(z):
            continue

        if pos == 0:
            if z < -entry_z:
                pos, e_lr, ei, e_pa, e_pb = 1, _df["log_ratio"].iloc[i], i, pa, pb
            elif z > entry_z:
                pos, e_lr, ei, e_pa, e_pb = -1, _df["log_ratio"].iloc[i], i, pa, pb
        else:
            ex = False
            if pos == 1 and z >= -exit_z:
                ex = True
            elif pos == -1 and z <= exit_z:
                ex = True
            elif abs(z) > stop_z:
                ex = True
            elif i - ei > max_hold:
                ex = True

            if ex:
                ra = (pa - e_pa) / e_pa
                rb = (pb - e_pb) / e_pb
                pnl = (ra - rb) * pos if pos == 1 else (rb - ra)
                pnl -= rt_fee
                days = (i - ei) / 24
                pnl -= days * 0.0001
                trades.append({"pnl": pnl * 100, "h": i - ei})
                pos = 0

    if not trades:
        return {"n": 0, "pnl": 0, "wr": 0, "sharpe": 0, "pf": 0, "avg_h": 0}

    pnls = np.array([t["pnl"] for t in trades])
    hs = np.array([t["h"] for t in trades])
    w = pnls[pnls > 0]
    l = pnls[pnls <= 0]
    return {
        "n": len(pnls),
        "pnl": pnls.sum(),
        "wr": (pnls > 0).mean() * 100,
        "sharpe": pnls.mean() / pnls.std() * np.sqrt(len(pnls)) if pnls.std() > 0 else 0,
        "pf": w.sum() / abs(l.sum()) if len(l) > 0 and l.sum() != 0 else float("inf"),
        "avg_h": hs.mean(),
    }


# -- 6. Multi-pair Screening ---------------------------------------------

CRYPTO_PAIRS = [
    ("BTC", "ETH"),
    ("BTC", "SOL"),
    ("ETH", "SOL"),
    ("BTC", "BNB"),
    ("ETH", "BNB"),
    ("SOL", "BNB"),
]


async def screen_all_pairs(months: int, tf: str):
    """Download and screen multiple pairs."""
    exchange = ccxt.binanceusdm({"enableRateLimit": True})

    # Download unique assets
    assets = sorted(set(a for pair in CRYPTO_PAIRS for a in pair))
    data = {}

    for asset in assets:
        cached = load_cached(asset, tf)
        if cached is not None and len(cached) > 1000:
            print(f"Using cached {asset}")
            data[asset] = cached
            continue

        sym = f"{asset}/USDT:USDT"
        since_ms = int((datetime.now(timezone.utc) - timedelta(days=months * 30)).timestamp() * 1000)
        print(f"Downloading {sym} {tf} ({months}m)...")
        all_c = []
        s = since_ms
        until = int(datetime.now(timezone.utc).timestamp() * 1000)
        while s < until:
            try:
                c = await exchange.fetch_ohlcv(sym, tf, since=s, limit=1500)
            except Exception as e:
                print(f"  Error: {e}")
                break
            if not c:
                break
            all_c.extend(c)
            last = c[-1][0]
            if last <= s:
                break
            s = last + 1
            await asyncio.sleep(0.05)

        df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        print(f"  Got {len(df)} candles")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(DATA_DIR / f"{asset}_{tf}.parquet")
        data[asset] = df

    await exchange.close()
    return data


# -- 7. Main --------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--all-pairs", action="store_true",
                        help="Screen BTC/ETH/SOL/BNB combinations")
    args = parser.parse_args()

    tf = args.timeframe

    if args.all_pairs or True:  # always screen all
        if args.skip_download:
            data = {}
            for asset in sorted(set(a for pair in CRYPTO_PAIRS for a in pair)):
                cached = load_cached(asset, tf)
                if cached is not None:
                    data[asset] = cached
                else:
                    print(f"Missing {asset}, will download")
        else:
            data = await screen_all_pairs(args.months, tf)

        # Screen all pairs
        print(f"\n{'='*70}")
        print(f"PAIR SCREENING")
        print(f"{'='*70}")
        print(f"  {'Pair':>10} {'Corr':>6} {'ADF_p':>8} {'Coint_p':>8} "
              f"{'HL(h)':>7} {'CointWin%':>9} {'Verdict':>10}")

        pair_results = []
        for a, b in CRYPTO_PAIRS:
            if a not in data or b not in data:
                continue

            df = build_ratio(data[a], data[b], a, b)
            if len(df) < 500:
                continue

            # Quick stats
            corr = df[a].pct_change().corr(df[b].pct_change())
            adf_p = adfuller(df["log_ratio"].dropna(), maxlag=48)[1]
            try:
                coint_p = coint(df["log_a"].values, df["log_b"].values)[1]
            except Exception:
                coint_p = 1.0

            lr = df["log_ratio"].values
            slope = sp_stats.linregress(lr[:-1], np.diff(lr))[0]
            hl = -np.log(2) / slope if slope < 0 else np.inf

            # Rolling coint
            window = 90 * 24
            step = 30 * 24
            coint_wins = 0
            coint_total = 0
            for start in range(0, len(df) - window, step):
                end = start + window
                sub = df.iloc[start:end]
                try:
                    _, p, _ = coint(sub["log_a"].values, sub["log_b"].values)
                    coint_total += 1
                    if p < 0.05:
                        coint_wins += 1
                except Exception:
                    pass
            coint_pct = coint_wins / coint_total * 100 if coint_total > 0 else 0

            hl_str = f"{hl:.0f}" if hl < 10000 else "inf"
            verdict = "GOOD" if (coint_p < 0.1 and hl < 500) else "MAYBE" if (coint_pct > 30) else "WEAK"

            print(f"  {a}/{b:>3} {corr:>6.3f} {adf_p:>8.4f} {coint_p:>8.4f} "
                  f"{hl_str:>7} {coint_pct:>8.0f}% {verdict:>10}")

            pair_results.append({
                "pair": f"{a}/{b}", "a": a, "b": b,
                "corr": corr, "coint_p": coint_p, "hl": hl,
                "coint_pct": coint_pct, "verdict": verdict, "df": df,
            })

        # Deep dive best pairs
        good_pairs = [p for p in pair_results if p["verdict"] in ("GOOD", "MAYBE")]
        if not good_pairs:
            good_pairs = sorted(pair_results, key=lambda x: x["coint_p"])[:2]

        for p in good_pairs:
            analyze_pair(p["df"], p["a"], p["b"])
            backtest_ratio(p["df"], p["a"], p["b"])
            scan_params(p["df"], p["a"], p["b"])

        # Summary
        print(f"\n{'='*70}")
        print("FINAL VERDICT")
        print(f"{'='*70}")
        for p in pair_results:
            bt = _bt_silent(p["df"], p["a"], p["b"], 168, 2.0, 0.5, 4.0, 0.05, 336)
            pnl_str = f"{bt['pnl']:+.2f}%" if bt["n"] > 0 else "N/A"
            sh_str = f"{bt['sharpe']:.2f}" if bt["n"] > 0 else "N/A"
            print(f"  {p['pair']:>8}: coint_p={p['coint_p']:.3f}, "
                  f"HL={p['hl']:.0f}h, "
                  f"rolling_coint={p['coint_pct']:.0f}%, "
                  f"BT: {bt['n']} trades, PnL={pnl_str}, Sharpe={sh_str}")


if __name__ == "__main__":
    asyncio.run(main())
