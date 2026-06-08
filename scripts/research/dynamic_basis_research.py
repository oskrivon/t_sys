"""
Dynamic Basis Research: Perp vs Spot spread timing.

Basis = (perp - spot) / spot. This IS the funding rate mechanism.
Question: are extreme basis events tradeable after costs?

MMs keep this tight. We're looking for:
1. How tight? (distribution of basis)
2. Extreme events — do they mean-revert fast enough?
3. After fees + slippage — is there edge?

Usage:
    python scripts/research/dynamic_basis_research.py [--symbol BTC] [--months 6]
"""

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

DATA_DIR = Path("data/raw/dynamic_basis")
FUNDING_DIR = Path("data/raw/funding/binance")


# -- 1. Download ----------------------------------------------------------

async def download_data(base: str, months: int = 6, timeframe: str = "5m"):
    """Download spot + perp at high frequency to capture basis dynamics."""
    exchange_spot = ccxt.binance({"enableRateLimit": True})
    exchange_perp = ccxt.binanceusdm({"enableRateLimit": True})

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=months * 30)).timestamp() * 1000)

    spot_sym = f"{base}/USDT"
    perp_sym = f"{base}/USDT:USDT"

    async def fetch_all(ex, sym, tf):
        all_c = []
        s = since_ms
        until = int(datetime.now(timezone.utc).timestamp() * 1000)
        while s < until:
            try:
                c = await ex.fetch_ohlcv(sym, tf, since=s, limit=1500)
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

    print(f"Downloading {spot_sym} spot {timeframe} ({months}m)...")
    df_spot = await fetch_all(exchange_spot, spot_sym, timeframe)
    print(f"  Got {len(df_spot)} candles")

    print(f"Downloading {perp_sym} perp {timeframe} ({months}m)...")
    df_perp = await fetch_all(exchange_perp, perp_sym, timeframe)
    print(f"  Got {len(df_perp)} candles")

    await exchange_spot.close()
    await exchange_perp.close()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df_spot.to_parquet(DATA_DIR / f"{base}_spot_{timeframe}.parquet")
    df_perp.to_parquet(DATA_DIR / f"{base}_perp_{timeframe}.parquet")

    return df_spot, df_perp


# -- 2. Analysis ----------------------------------------------------------

def build_basis(df_spot: pd.DataFrame, df_perp: pd.DataFrame) -> pd.DataFrame:
    """Merge spot + perp, calculate basis."""
    spot = df_spot[["ts", "close", "volume"]].rename(
        columns={"close": "spot", "volume": "spot_vol"}
    )
    perp = df_perp[["ts", "close", "volume"]].rename(
        columns={"close": "perp", "volume": "perp_vol"}
    )
    df = pd.merge(spot, perp, on="ts", how="inner")
    df["basis_abs"] = df["perp"] - df["spot"]
    df["basis_bps"] = (df["perp"] - df["spot"]) / df["spot"] * 10000  # basis points
    return df


def analyze_basis(df: pd.DataFrame, base: str, tf: str):
    """Full analysis of basis distribution and dynamics."""
    basis = df["basis_bps"]

    print(f"\n{'='*70}")
    print(f"Dynamic Basis Analysis: {base} ({tf})")
    print(f"{'='*70}")
    print(f"  Period: {df['ts'].iloc[0].strftime('%Y-%m-%d')} -> {df['ts'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  Observations: {len(df):,}")

    # Distribution
    print(f"\n  Basis distribution (bps):")
    print(f"    Mean:   {basis.mean():+.2f}")
    print(f"    Median: {basis.median():+.2f}")
    print(f"    Std:    {basis.std():.2f}")
    print(f"    P1:     {basis.quantile(0.01):+.2f}")
    print(f"    P5:     {basis.quantile(0.05):+.2f}")
    print(f"    P95:    {basis.quantile(0.95):+.2f}")
    print(f"    P99:    {basis.quantile(0.99):+.2f}")
    print(f"    Min:    {basis.min():+.2f}")
    print(f"    Max:    {basis.max():+.2f}")

    # How often is basis "extreme"?
    print(f"\n  Basis regime frequency:")
    for threshold in [1, 2, 3, 5, 10, 20, 50]:
        pct_above = (basis > threshold).mean() * 100
        pct_below = (basis < -threshold).mean() * 100
        print(f"    |basis| > {threshold:>2} bps: {(pct_above+pct_below):.2f}%  "
              f"(above: {pct_above:.2f}%, below: {pct_below:.2f}%)")

    # Autocorrelation — how fast does basis revert?
    print(f"\n  Mean-reversion speed:")
    for lag_name, lag in [("5m", 1), ("15m", 3), ("1h", 12), ("4h", 48), ("24h", 288)]:
        ac = basis.autocorr(lag)
        print(f"    AC({lag_name:>3}): {ac:.4f}")

    # Conditional mean-reversion: what happens after extreme basis?
    print(f"\n  Conditional returns after extreme basis (bps):")
    print(f"    {'Event':>15} {'Count':>6} {'T+5m':>7} {'T+15m':>7} {'T+1h':>7} {'T+4h':>7}")

    for threshold, label in [(5, "basis > 5"), (10, "basis > 10"), (20, "basis > 20"),
                              (-5, "basis < -5"), (-10, "basis < -10"), (-20, "basis < -20")]:
        if threshold > 0:
            mask = basis > threshold
        else:
            mask = basis < threshold

        count = mask.sum()
        if count < 5:
            continue

        # Future basis change (mean-reversion = basis shrinks)
        fwd = {}
        for periods, name in [(1, "T+5m"), (3, "T+15m"), (12, "T+1h"), (48, "T+4h")]:
            future_basis = basis.shift(-periods)
            change = future_basis - basis
            fwd[name] = change[mask].mean()

        print(f"    {label:>15} {count:>6} "
              f"{fwd.get('T+5m', 0):>+6.2f} {fwd.get('T+15m', 0):>+6.2f} "
              f"{fwd.get('T+1h', 0):>+6.2f} {fwd.get('T+4h', 0):>+6.2f}")

    return basis


# -- 3. Backtest ----------------------------------------------------------

def backtest_basis(df: pd.DataFrame, base: str, tf: str,
                   entry_bps: float = 10.0,
                   exit_bps: float = 2.0,
                   stop_bps: float = 50.0,
                   fee_bps: float = 5.0,  # taker on perp + spot
                   ):
    """
    Trade extreme basis events.

    When basis > entry_bps: short perp + buy spot (expect basis to shrink)
    When basis < -entry_bps: long perp + sell spot (expect basis to widen)
    Exit when |basis| < exit_bps or |basis| > stop_bps.

    Total fee = 2 legs * 2 (open+close) = 4 trades.
    Spot taker ~10bps, Perp taker ~5bps.
    """
    # Realistic fees: spot taker 0.1%, perp taker 0.05%, per side
    # Round trip: (0.1 + 0.05) * 2 = 0.3% = 30 bps
    # With maker: (0.02 + 0.02) * 2 = 0.08% = 8 bps
    rt_fee_bps = fee_bps * 2  # open + close, both legs

    print(f"\n{'-'*70}")
    print(f"Basis Backtest: {base} ({tf})")
    print(f"  Entry: |basis| > {entry_bps} bps, Exit: |basis| < {exit_bps} bps")
    print(f"  Stop: |basis| > {stop_bps} bps")
    print(f"  Round-trip fees: {rt_fee_bps:.0f} bps (per-leg: {fee_bps:.0f} bps)")
    print(f"{'-'*70}")

    basis = df["basis_bps"].values
    n = len(basis)

    position = 0  # +1 = short basis (short perp, long spot), -1 = long basis
    trades = []
    entry_val = 0.0
    entry_idx = 0

    for i in range(n):
        b = basis[i]
        if np.isnan(b):
            continue

        if position == 0:
            if b > entry_bps:
                position = 1  # short basis
                entry_val = b
                entry_idx = i
            elif b < -entry_bps:
                position = -1  # long basis
                entry_val = b
                entry_idx = i
        else:
            exit_signal = False
            exit_reason = ""

            if position == 1:  # short basis, want it to shrink
                if b <= exit_bps:
                    exit_signal, exit_reason = True, "target"
                elif b > stop_bps:
                    exit_signal, exit_reason = True, "stop"
            else:  # long basis, want it to grow
                if b >= -exit_bps:
                    exit_signal, exit_reason = True, "target"
                elif b < -stop_bps:
                    exit_signal, exit_reason = True, "stop"

            # Max hold time: 4 hours
            if i - entry_idx > 48:  # 48 * 5min = 4h
                exit_signal, exit_reason = True, "timeout"

            if exit_signal:
                pnl_bps = (entry_val - b) * position - rt_fee_bps
                trades.append({
                    "entry_ts": df["ts"].iloc[entry_idx],
                    "exit_ts": df["ts"].iloc[i],
                    "direction": "short_basis" if position == 1 else "long_basis",
                    "entry_bps": entry_val,
                    "exit_bps": b,
                    "pnl_bps": pnl_bps,
                    "duration_bars": i - entry_idx,
                    "exit_reason": exit_reason,
                })
                position = 0

    if not trades:
        print("  No trades!")
        return

    tdf = pd.DataFrame(trades)
    tdf["duration_min"] = tdf["duration_bars"] * 5  # 5m bars

    winners = tdf[tdf["pnl_bps"] > 0]
    losers = tdf[tdf["pnl_bps"] <= 0]

    total = tdf["pnl_bps"].sum()
    wr = len(winners) / len(tdf) * 100
    avg = tdf["pnl_bps"].mean()
    pf = winners["pnl_bps"].sum() / abs(losers["pnl_bps"].sum()) if len(losers) > 0 and losers["pnl_bps"].sum() != 0 else float("inf")
    sharpe = avg / tdf["pnl_bps"].std() * np.sqrt(len(tdf)) if tdf["pnl_bps"].std() > 0 else 0

    # Max DD
    cum = tdf["pnl_bps"].cumsum()
    dd = (cum - cum.cummax()).min()

    print(f"\n  Results:")
    print(f"    Trades:       {len(tdf)}")
    print(f"    Win rate:     {wr:.1f}%")
    print(f"    Total PnL:    {total:+.1f} bps")
    print(f"    Avg PnL:      {avg:+.2f} bps")
    print(f"    Profit factor:{pf:.2f}")
    print(f"    Sharpe:       {sharpe:.2f}")
    print(f"    Max DD:       {dd:+.1f} bps")
    print(f"    Avg duration: {tdf['duration_min'].mean():.0f} min")

    # By direction
    print(f"\n  By direction:")
    for d in ["short_basis", "long_basis"]:
        sub = tdf[tdf["direction"] == d]
        if len(sub) > 0:
            print(f"    {d:15s}: {len(sub)} trades, "
                  f"WR={len(sub[sub['pnl_bps']>0])/len(sub)*100:.0f}%, "
                  f"PnL={sub['pnl_bps'].sum():+.1f} bps")

    # By exit reason
    print(f"\n  By exit reason:")
    for reason, group in tdf.groupby("exit_reason"):
        print(f"    {reason:10s}: {len(group)} trades, "
              f"WR={len(group[group['pnl_bps']>0])/len(group)*100:.0f}%, "
              f"PnL={group['pnl_bps'].sum():+.1f} bps")

    # Last trades
    print(f"\n  Last 5 trades:")
    for _, t in tdf.tail(5).iterrows():
        print(f"    {t['entry_ts'].strftime('%m-%d %H:%M')} -> "
              f"{t['exit_ts'].strftime('%m-%d %H:%M')} "
              f"{t['direction']:15s} "
              f"basis={t['entry_bps']:+.1f}->{t['exit_bps']:+.1f} "
              f"pnl={t['pnl_bps']:+.1f}bps ({t['duration_min']:.0f}min) "
              f"[{t['exit_reason']}]")

    return tdf


def scan_thresholds(df, base, tf):
    """Quick scan of entry thresholds and fee assumptions."""
    print(f"\n{'='*70}")
    print(f"Threshold Scan: {base}")
    print(f"{'='*70}")
    print(f"  {'Entry':>6} {'Exit':>5} {'Fee':>5} {'Trades':>6} {'WR%':>5} "
          f"{'PnL':>8} {'Avg':>7} {'Sharpe':>7}")

    basis = df["basis_bps"].values
    n = len(basis)

    for entry_bps in [5, 10, 15, 20, 30]:
        for fee_label, fee_bps in [("maker", 4), ("taker", 15)]:
            rt_fee = fee_bps * 2
            # Quick silent backtest
            pos = 0
            trades = []
            ev = 0.0
            ei = 0
            for i in range(n):
                b = basis[i]
                if np.isnan(b):
                    continue
                if pos == 0:
                    if b > entry_bps:
                        pos, ev, ei = 1, b, i
                    elif b < -entry_bps:
                        pos, ev, ei = -1, b, i
                else:
                    exit_sig = False
                    if pos == 1 and (b <= 2 or b > 50 or i - ei > 48):
                        exit_sig = True
                    elif pos == -1 and (b >= -2 or b < -50 or i - ei > 48):
                        exit_sig = True
                    if exit_sig:
                        pnl = (ev - b) * pos - rt_fee
                        trades.append(pnl)
                        pos = 0

            if len(trades) < 3:
                continue
            pnls = np.array(trades)
            wr = (pnls > 0).mean() * 100
            tot = pnls.sum()
            avg = pnls.mean()
            sh = avg / pnls.std() * np.sqrt(len(pnls)) if pnls.std() > 0 else 0
            print(f"  {entry_bps:>5}bp {2:>4}bp {fee_label:>5} {len(pnls):>6} {wr:>4.0f}% "
                  f"{tot:>+7.0f}bp {avg:>+6.1f}bp {sh:>6.2f}")


# -- 4. Main --------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()

    base = args.symbol.upper()
    tf = args.timeframe

    if args.skip_download:
        spot_path = DATA_DIR / f"{base}_spot_{tf}.parquet"
        perp_path = DATA_DIR / f"{base}_perp_{tf}.parquet"
        if not spot_path.exists():
            print(f"No cached data. Run without --skip-download first.")
            return
        df_spot = pd.read_parquet(spot_path)
        df_perp = pd.read_parquet(perp_path)
    else:
        df_spot, df_perp = await download_data(base, args.months, tf)

    df = build_basis(df_spot, df_perp)
    print(f"\nMerged: {len(df):,} rows")

    # 1. Distribution analysis
    analyze_basis(df, base, tf)

    # 2. Backtest with different fee assumptions
    print(f"\n  [Maker fees scenario: 4 bps per leg]")
    backtest_basis(df, base, tf, entry_bps=10, fee_bps=4)

    print(f"\n  [Taker fees scenario: 15 bps per leg]")
    backtest_basis(df, base, tf, entry_bps=10, fee_bps=15)

    # 3. Threshold scan
    scan_thresholds(df, base, tf)

    # 4. Verdict
    print(f"\n{'='*70}")
    print("VERDICT")
    print(f"{'='*70}")
    print("""
  The question is simple:
  - How often does basis exceed round-trip fees?
  - When it does, does it revert fast enough?

  If basis rarely exceeds 10 bps and fees are 8-30 bps -> no edge.
  MMs keep this tight because it's the simplest arb in crypto.
    """)


if __name__ == "__main__":
    asyncio.run(main())
