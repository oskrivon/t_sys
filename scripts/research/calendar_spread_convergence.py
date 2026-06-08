"""
Calendar Spread Convergence: basis guaranteed -> 0 at expiry.

Strategy: short quarterly + long perp when basis is "rich",
ride convergence to zero. Account for funding costs on perp leg.

Key model: expected basis = f(days_to_expiry, rate).
Enter when actual basis > expected + threshold.

Usage:
    python scripts/research/calendar_spread_convergence.py [--symbol BTC]
    python scripts/research/calendar_spread_convergence.py --symbol ETH --with-funding
"""

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit

DATA_DIR = Path("data/raw/calendar_spread")
FUNDING_DIR = Path("data/raw/funding/binance")


# -- 1. Load Data --------------------------------------------------------

def load_spread_data(base: str, timeframe: str = "1h") -> list[dict]:
    """Load cached perp + quarterly data, build spread frames."""
    perp_path = DATA_DIR / f"{base}_perp_{timeframe}.parquet"
    if not perp_path.exists():
        raise FileNotFoundError(
            f"No data at {perp_path}. Run calendar_spread_research.py first."
        )

    df_perp = pd.read_parquet(perp_path)

    results = []
    for f in sorted(DATA_DIR.glob(f"{base}*USDT-*_{timeframe}.parquet")):
        df_q = pd.read_parquet(f)
        # Extract expiry from data: last timestamp + some days (approx)
        # Better: parse from filename
        parts = f.stem.split("-")
        # filename like: BTC_USDT_USDT-260626_1h
        expiry_str = parts[-1].split("_")[0]  # "260626"
        expiry_dt = datetime.strptime(f"20{expiry_str}", "%Y%m%d").replace(
            hour=8, tzinfo=timezone.utc  # Binance settles 08:00 UTC
        )

        # Merge
        perp = df_perp[["ts", "close", "volume"]].rename(
            columns={"close": "perp_close", "volume": "perp_vol"}
        )
        q = df_q[["ts", "close", "volume"]].rename(
            columns={"close": "q_close", "volume": "q_vol"}
        )
        df = pd.merge(perp, q, on="ts", how="inner")

        df["basis_abs"] = df["q_close"] - df["perp_close"]
        df["basis_pct"] = df["basis_abs"] / df["perp_close"] * 100
        df["days_to_expiry"] = (expiry_dt - df["ts"]).dt.total_seconds() / 86400
        df["basis_ann"] = df["basis_pct"] / df["days_to_expiry"].clip(lower=0.01) * 365
        df = df[df["days_to_expiry"] > 0.5].copy()

        sym = f.stem.replace(f"_{timeframe}", "").replace("_", "/", 1).replace("_", ":", 1)
        results.append({
            "symbol": sym,
            "expiry": expiry_dt,
            "df": df,
        })

    return results


def load_funding(base: str) -> pd.DataFrame | None:
    """Load historical funding rates for perp leg cost estimation."""
    path = FUNDING_DIR / f"{base}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return df[["ts", "funding_rate"]].sort_values("ts")


# -- 2. Convergence Model ------------------------------------------------

def fit_convergence_curve(df: pd.DataFrame) -> dict:
    """
    Fit basis_pct = a * days_to_expiry^b  (power law decay to zero).

    Alternatives considered:
    - Linear: basis = a * dte  (too simple, ignores convexity)
    - Exponential: basis = a * exp(-b * dte)  (wrong direction)
    - Power law: captures the convex shape well
    """
    dte = df["days_to_expiry"].values
    basis = df["basis_pct"].values

    # Power law: basis = a * dte^b
    def power_law(x, a, b):
        return a * np.power(x, b)

    try:
        popt, pcov = curve_fit(power_law, dte, basis, p0=[0.01, 1.0], maxfev=5000)
        a, b = popt
        predicted = power_law(dte, a, b)
        residuals = basis - predicted
        ss_res = np.sum(residuals ** 2)
        ss_tot = np.sum((basis - basis.mean()) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    except Exception:
        # Fallback: linear
        a, b = np.polyfit(dte, basis, 1)
        predicted = a * dte + b
        residuals = basis - predicted
        r2 = 0
        a, b = a, 1.0  # mark as linear

    # Linear fit for comparison
    slope, intercept = np.polyfit(dte, basis, 1)
    linear_pred = slope * dte + intercept
    linear_resid = basis - linear_pred
    ss_res_lin = np.sum(linear_resid ** 2)
    ss_tot_lin = np.sum((basis - basis.mean()) ** 2)
    r2_lin = 1 - ss_res_lin / ss_tot_lin if ss_tot_lin > 0 else 0

    return {
        "a": popt[0] if 'popt' in dir() else a,
        "b": popt[1] if 'popt' in dir() else b,
        "r2_power": r2,
        "r2_linear": r2_lin,
        "residuals": residuals,
        "predicted": predicted,
        "linear_slope": slope,
        "linear_intercept": intercept,
    }


def analyze_convergence(df: pd.DataFrame, label: str) -> dict:
    """Analyze how basis converges to zero vs days-to-expiry."""
    print(f"\n{'='*70}")
    print(f"Convergence Analysis: {label}")
    print(f"{'='*70}")

    dte = df["days_to_expiry"]
    basis = df["basis_pct"]

    print(f"  Period: {df['ts'].iloc[0].strftime('%Y-%m-%d')} -> {df['ts'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  DTE range: {dte.min():.0f} -> {dte.max():.0f} days")
    print(f"  Observations: {len(df)}")

    # Basis by DTE bucket
    print(f"\n  Basis by days-to-expiry:")
    print(f"  {'DTE':>8} {'Mean%':>8} {'Std%':>8} {'Min%':>8} {'Max%':>8} {'Ann%':>8}")
    for lo, hi in [(0, 7), (7, 14), (14, 30), (30, 60), (60, 90), (90, 180)]:
        mask = (dte >= lo) & (dte < hi)
        if mask.sum() < 10:
            continue
        sub = basis[mask]
        ann = df.loc[mask, "basis_ann"]
        print(f"  {lo:>3}-{hi:<3}d {sub.mean():>+7.4f} {sub.std():>7.4f} "
              f"{sub.min():>+7.4f} {sub.max():>+7.4f} {ann.mean():>+7.1f}")

    # Fit convergence curve
    fit = fit_convergence_curve(df)
    print(f"\n  Convergence fit:")
    print(f"    Power law: basis = {fit['a']:.6f} * dte^{fit['b']:.3f}  (R2={fit['r2_power']:.4f})")
    print(f"    Linear:    basis = {fit['linear_slope']:.6f} * dte + {fit['linear_intercept']:.4f}  (R2={fit['r2_linear']:.4f})")

    # Residuals analysis (deviation from fair value)
    resid = fit["residuals"]
    print(f"\n  Residuals (actual - expected):")
    print(f"    Mean:  {resid.mean():+.5f}%")
    print(f"    Std:   {resid.std():.5f}%")
    print(f"    Skew:  {pd.Series(resid).skew():.2f}")

    # Autocorrelation of residuals
    resid_s = pd.Series(resid)
    ac1 = resid_s.autocorr(1)
    ac4 = resid_s.autocorr(4)
    ac24 = resid_s.autocorr(24)
    print(f"    AC(1h): {ac1:.3f}, AC(4h): {ac4:.3f}, AC(24h): {ac24:.3f}")
    print(f"    -> {'High persistence' if ac1 > 0.95 else 'Moderate persistence' if ac1 > 0.8 else 'Low persistence'}")

    return fit


# -- 3. Convergence Backtest ---------------------------------------------

def backtest_convergence(
    df: pd.DataFrame,
    label: str,
    fit: dict,
    funding_df: pd.DataFrame | None = None,
    # Strategy params
    entry_richness: float = 1.5,   # enter when basis > expected * entry_richness
    exit_richness: float = 1.0,    # exit when basis <= expected * exit_richness
    min_dte: int = 3,              # don't enter within N days of expiry
    max_dte: int = 90,             # don't enter too far out
    min_basis_ann: float = 3.0,    # minimum annualized basis to enter (%)
    fee_pct: float = 0.02,         # fee per leg in % (2 bps)
    leverage: int = 1,             # leverage on each leg
) -> dict:
    """
    Convergence trade backtest.

    Entry: short quarterly + long perp when basis is "rich" vs expected curve.
    Exit: when basis reverts to or below expected curve.
    PnL: basis_captured * leverage - fees - funding_cost.
    """
    print(f"\n{'-'*70}")
    print(f"Convergence Backtest: {label}")
    print(f"  Entry: basis > {entry_richness}x expected, Exit: basis <= {exit_richness}x expected")
    print(f"  DTE: {min_dte}-{max_dte} days, Min ann basis: {min_basis_ann}%")
    print(f"  Fees: {fee_pct*100:.1f}bps/leg, Leverage: {leverage}x")
    print(f"{'-'*70}")

    # Calculate expected basis for each row
    df = df.copy()
    dte = df["days_to_expiry"].values
    df["expected_basis"] = fit["a"] * np.power(dte, fit["b"])
    df["richness"] = df["basis_pct"] / df["expected_basis"].clip(lower=0.001)

    # Merge funding if available
    avg_funding_8h = 0.0
    if funding_df is not None and len(funding_df) > 0:
        # Resample funding to approximate cost per hour
        funding_df = funding_df.set_index("ts").sort_index()
        avg_funding_8h = funding_df["funding_rate"].mean()  # avg per 8h settlement
        avg_funding_1h = avg_funding_8h / 8  # approx per hour
        print(f"  Avg funding rate: {avg_funding_8h*100:.4f}% per 8h ({avg_funding_8h*3*365*100:.1f}% APR)")
    else:
        avg_funding_1h = 0.01 / 100 / 8  # assume 0.01% per 8h as default
        print(f"  Using default funding: 0.01% per 8h")

    # Simulate
    position = 0  # 0 = flat, 1 = in convergence trade
    trades = []
    entry_basis = 0.0
    entry_idx = 0

    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        dte_now = row["days_to_expiry"]
        basis_now = row["basis_pct"]
        expected = row["expected_basis"]
        richness = row["richness"]
        ann_basis = row["basis_ann"]

        if position == 0:
            # Entry conditions
            if (dte_now >= min_dte
                and dte_now <= max_dte
                and richness > entry_richness
                and ann_basis > min_basis_ann
                and basis_now > 0):  # only short spread when positive basis
                position = 1
                entry_basis = basis_now
                entry_idx = i
                entry_expected = expected

        else:
            # Exit conditions
            exit_reason = None
            if richness <= exit_richness:
                exit_reason = "converged"
            elif dte_now < min_dte:
                exit_reason = "expiry_close"
            elif basis_now < 0:
                exit_reason = "negative_basis"

            if exit_reason:
                # PnL calculation
                basis_captured = entry_basis - basis_now  # positive = profit
                duration_h = i - entry_idx
                funding_cost = avg_funding_1h * duration_h * 100  # cost in % terms
                # Funding: we're LONG perp. If funding positive, we pay. If negative, we receive.
                # On average in bull market, funding is positive -> cost.
                fee_cost = fee_pct * 2  # round trip on both legs

                pnl_gross = basis_captured * leverage
                pnl_net = pnl_gross - fee_cost - funding_cost * leverage

                trades.append({
                    "entry_ts": df["ts"].iloc[entry_idx],
                    "exit_ts": df["ts"].iloc[i],
                    "entry_basis": entry_basis,
                    "exit_basis": basis_now,
                    "entry_dte": df["days_to_expiry"].iloc[entry_idx],
                    "exit_dte": dte_now,
                    "entry_richness": df["richness"].iloc[entry_idx],
                    "exit_richness": richness,
                    "basis_captured": basis_captured,
                    "funding_cost": funding_cost,
                    "fee_cost": fee_cost,
                    "pnl_gross": pnl_gross,
                    "pnl_net": pnl_net,
                    "duration_h": duration_h,
                    "exit_reason": exit_reason,
                })
                position = 0

    # Force close if still in position
    if position == 1:
        i = n - 1
        basis_now = df["basis_pct"].iloc[i]
        duration_h = i - entry_idx
        basis_captured = entry_basis - basis_now
        funding_cost = avg_funding_1h * duration_h * 100
        fee_cost = fee_pct * 2
        pnl_gross = basis_captured * leverage
        pnl_net = pnl_gross - fee_cost - funding_cost * leverage
        trades.append({
            "entry_ts": df["ts"].iloc[entry_idx],
            "exit_ts": df["ts"].iloc[i],
            "entry_basis": entry_basis,
            "exit_basis": basis_now,
            "entry_dte": df["days_to_expiry"].iloc[entry_idx],
            "exit_dte": df["days_to_expiry"].iloc[i],
            "entry_richness": df["richness"].iloc[entry_idx],
            "exit_richness": df["richness"].iloc[i],
            "basis_captured": basis_captured,
            "funding_cost": funding_cost,
            "fee_cost": fee_cost,
            "pnl_gross": pnl_gross,
            "pnl_net": pnl_net,
            "duration_h": duration_h,
            "exit_reason": "still_open",
        })

    if not trades:
        print("  No trades!")
        return {"n_trades": 0}

    tdf = pd.DataFrame(trades)
    _print_results(tdf, leverage)

    return {
        "n_trades": len(tdf),
        "trades_df": tdf,
        "total_pnl_net": tdf["pnl_net"].sum(),
        "sharpe": _sharpe(tdf["pnl_net"]),
    }


def _print_results(tdf: pd.DataFrame, leverage: int):
    """Print backtest results."""
    winners = tdf[tdf["pnl_net"] > 0]
    losers = tdf[tdf["pnl_net"] <= 0]

    total_gross = tdf["pnl_gross"].sum()
    total_net = tdf["pnl_net"].sum()
    total_funding = tdf["funding_cost"].sum()
    total_fees = tdf["fee_cost"].sum()
    win_rate = len(winners) / len(tdf) * 100 if len(tdf) > 0 else 0

    gross_win = winners["pnl_net"].sum() if len(winners) > 0 else 0
    gross_loss = abs(losers["pnl_net"].sum()) if len(losers) > 0 else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown
    cum = tdf["pnl_net"].cumsum()
    dd = (cum - cum.cummax()).min()

    # Time in market
    total_hours = tdf["duration_h"].sum()
    total_possible = (tdf["exit_ts"].max() - tdf["entry_ts"].min()).total_seconds() / 3600
    utilization = total_hours / total_possible * 100 if total_possible > 0 else 0

    print(f"\n  Results ({leverage}x leverage):")
    print(f"    Trades:        {len(tdf)}")
    print(f"    Win rate:      {win_rate:.1f}%")
    print(f"    PnL gross:     {total_gross:+.4f}%")
    print(f"    Funding cost:  {total_funding:.4f}%")
    print(f"    Fee cost:      {total_fees:.4f}%")
    print(f"    PnL NET:       {total_net:+.4f}%")
    print(f"    Avg PnL/trade: {tdf['pnl_net'].mean():+.4f}%")
    print(f"    Profit factor: {pf:.2f}")
    print(f"    Sharpe:        {_sharpe(tdf['pnl_net']):.2f}")
    print(f"    Max DD:        {dd:+.4f}%")
    print(f"    Avg duration:  {tdf['duration_h'].mean():.0f}h")
    print(f"    Utilization:   {utilization:.1f}%")

    # By exit reason
    print(f"\n  Exit reasons:")
    for reason, group in tdf.groupby("exit_reason"):
        print(f"    {reason:15s}: {len(group)} trades, "
              f"PnL={group['pnl_net'].sum():+.4f}%, "
              f"WR={len(group[group['pnl_net']>0])/len(group)*100:.0f}%")

    # Last trades
    print(f"\n  Last 5 trades:")
    for _, t in tdf.tail(5).iterrows():
        print(f"    {t['entry_ts'].strftime('%m-%d %H:%M')} -> "
              f"{t['exit_ts'].strftime('%m-%d %H:%M')} "
              f"dte={t['entry_dte']:.0f}->{t['exit_dte']:.0f}d "
              f"basis={t['entry_basis']:+.3f}->{t['exit_basis']:+.3f}% "
              f"net={t['pnl_net']:+.4f}% [{t['exit_reason']}] "
              f"({t['duration_h']:.0f}h)")


def _sharpe(pnls):
    if len(pnls) < 2 or pnls.std() == 0:
        return 0
    return pnls.mean() / pnls.std() * np.sqrt(len(pnls))


# -- 4. Parameter Scan ---------------------------------------------------

def scan_parameters(df, label, fit, funding_df):
    """Scan richness thresholds and DTE ranges."""
    print(f"\n{'='*70}")
    print(f"Parameter Scan: {label}")
    print(f"{'='*70}")

    header = (f"  {'Rich':>5} {'Exit':>5} {'MinDTE':>6} {'MaxDTE':>6} "
              f"{'Trades':>6} {'WR%':>5} {'Gross%':>8} {'Fund%':>7} "
              f"{'NET%':>8} {'Sharpe':>7} {'AvgH':>5}")
    print(header)

    best = None
    for entry_rich in [1.2, 1.5, 2.0, 2.5]:
        for exit_rich in [0.8, 1.0]:
            for min_dte in [3, 7]:
                r = _backtest_silent(df, fit, funding_df,
                                     entry_rich, exit_rich, min_dte, 90)
                if r["n_trades"] < 3:
                    continue
                t = r["tdf"]
                wr = len(t[t["pnl_net"] > 0]) / len(t) * 100
                sharpe = _sharpe(t["pnl_net"])
                print(f"  {entry_rich:>5.1f} {exit_rich:>5.1f} {min_dte:>6} {90:>6} "
                      f"{len(t):>6} {wr:>4.0f}% {t['pnl_gross'].sum():>+7.4f} "
                      f"{t['funding_cost'].sum():>6.4f} "
                      f"{t['pnl_net'].sum():>+7.4f} {sharpe:>6.2f} "
                      f"{t['duration_h'].mean():>4.0f}")
                if best is None or sharpe > best.get("sharpe", -999):
                    best = {"entry_rich": entry_rich, "exit_rich": exit_rich,
                            "min_dte": min_dte, "sharpe": sharpe,
                            "net_pnl": t["pnl_net"].sum(), "n": len(t)}

    if best:
        print(f"\n  Best: rich={best['entry_rich']}, exit={best['exit_rich']}, "
              f"min_dte={best['min_dte']}, Sharpe={best['sharpe']:.2f}, "
              f"Net={best['net_pnl']:+.4f}%")
    return best


def _backtest_silent(df, fit, funding_df, entry_rich, exit_rich, min_dte, max_dte):
    """Silent backtest for parameter scan."""
    _df = df.copy()
    dte = _df["days_to_expiry"].values
    _df["expected_basis"] = fit["a"] * np.power(dte, fit["b"])
    _df["richness"] = _df["basis_pct"] / _df["expected_basis"].clip(lower=0.001)

    # Funding
    if funding_df is not None and len(funding_df) > 0:
        avg_funding_1h = funding_df["funding_rate"].mean() / 8
    else:
        avg_funding_1h = 0.01 / 100 / 8

    fee_pct = 0.02
    position = 0
    trades = []
    entry_basis = 0.0
    entry_idx = 0

    for i in range(len(_df)):
        row = _df.iloc[i]
        dte_now = row["days_to_expiry"]
        basis_now = row["basis_pct"]
        richness = row["richness"]
        ann_basis = row["basis_ann"]

        if position == 0:
            if (dte_now >= min_dte and dte_now <= max_dte
                and richness > entry_rich and ann_basis > 3.0 and basis_now > 0):
                position = 1
                entry_basis = basis_now
                entry_idx = i
        else:
            exit_signal = (richness <= exit_rich or dte_now < min_dte or basis_now < 0)
            if exit_signal:
                dur = i - entry_idx
                cap = entry_basis - basis_now
                fund = avg_funding_1h * dur * 100
                pnl_g = cap
                pnl_n = pnl_g - fee_pct * 2 - fund
                trades.append({
                    "pnl_gross": pnl_g, "pnl_net": pnl_n,
                    "funding_cost": fund, "fee_cost": fee_pct * 2,
                    "duration_h": dur,
                })
                position = 0

    if not trades:
        return {"n_trades": 0, "tdf": pd.DataFrame()}
    tdf = pd.DataFrame(trades)
    return {"n_trades": len(tdf), "tdf": tdf}


# -- 5. Realistic Cost Analysis ------------------------------------------

def cost_analysis(df: pd.DataFrame, funding_df: pd.DataFrame | None, label: str):
    """Break down realistic costs of the convergence trade."""
    print(f"\n{'='*70}")
    print(f"Cost Analysis: {label}")
    print(f"{'='*70}")

    basis = df["basis_pct"]
    ann = df["basis_ann"]

    # 1. Fees
    maker_fee = 0.02  # Binance USDT-M maker
    taker_fee = 0.05  # Binance USDT-M taker
    print(f"\n  1. Trading Fees (Binance USDT-M):")
    print(f"     Maker: {maker_fee*100:.0f} bps, Taker: {taker_fee*100:.0f} bps")
    print(f"     Round-trip (2 legs, maker): {maker_fee*2*2*100:.0f} bps = {maker_fee*2*2:.3f}%")
    print(f"     Round-trip (2 legs, taker): {taker_fee*2*2*100:.0f} bps = {taker_fee*2*2:.3f}%")

    # 2. Funding
    if funding_df is not None and len(funding_df) > 0:
        fr = funding_df["funding_rate"]
        avg_8h = fr.mean()
        daily = avg_8h * 3
        monthly = daily * 30
        apr = daily * 365
        print(f"\n  2. Funding Cost (long perp leg):")
        print(f"     Avg per 8h: {avg_8h*100:.4f}%")
        print(f"     Daily:      {daily*100:.4f}%")
        print(f"     Monthly:    {monthly*100:.3f}%")
        print(f"     APR:        {apr*100:.2f}%")
        print(f"     (Positive = you PAY when long perp)")
    else:
        print(f"\n  2. Funding: no data, using 0.01%/8h estimate")
        monthly = 0.0001 * 3 * 30

    # 3. Basis income vs costs
    print(f"\n  3. Basis vs Costs comparison:")
    for dte_bucket in [90, 60, 30, 14]:
        mask = df["days_to_expiry"].between(dte_bucket - 5, dte_bucket + 5)
        if mask.sum() < 10:
            continue
        avg_basis = basis[mask].mean()
        # Expected convergence profit = avg_basis (captured over holding period)
        # Cost = funding * holding_days + fees
        hold_days = dte_bucket  # hold to expiry
        funding_cost_pct = monthly / 30 * hold_days if funding_df is not None else 0.0001 * 3 * hold_days
        fee_cost_pct = maker_fee * 2 * 2  # 2 legs * 2 (open+close) * maker
        total_cost = funding_cost_pct * 100 + fee_cost_pct
        net = avg_basis - total_cost
        print(f"     DTE ~{dte_bucket:>3}d: basis={avg_basis:+.3f}%, "
              f"funding={funding_cost_pct*100:.3f}%, fees={fee_cost_pct:.3f}%, "
              f"NET={net:+.3f}%")

    # 4. Slippage estimate
    print(f"\n  4. Quarterly volume vs perp:")
    q_vol = df["q_vol"].mean()
    p_vol = df["perp_vol"].mean()
    ratio = q_vol / p_vol * 100 if p_vol > 0 else 0
    print(f"     Quarterly avg vol: {q_vol:,.0f}")
    print(f"     Perp avg vol:     {p_vol:,.0f}")
    print(f"     Ratio:            {ratio:.1f}%")
    print(f"     -> {'OK liquidity' if ratio > 10 else 'LOW liquidity - slippage risk!'}")


# -- 6. Main -------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--with-funding", action="store_true",
                        help="Include funding cost from historical data")
    args = parser.parse_args()

    base = args.symbol.upper()
    tf = args.timeframe

    # Load data
    print(f"Loading {base} calendar spread data...")
    try:
        spreads = load_spread_data(base, tf)
    except FileNotFoundError as e:
        print(e)
        return

    funding_df = load_funding(base) if args.with_funding else None
    if args.with_funding and funding_df is not None:
        print(f"Loaded {len(funding_df)} funding records")
    elif args.with_funding:
        print("No funding data found, using default estimate")

    for spread_data in spreads:
        sym = spread_data["symbol"]
        df = spread_data["df"]
        expiry = spread_data["expiry"]

        if len(df) < 200:
            print(f"\nSkipping {sym}: only {len(df)} obs")
            continue

        # 1. Convergence analysis
        fit = analyze_convergence(df, f"{sym} (exp {expiry.strftime('%Y-%m-%d')})")

        # 2. Cost analysis
        cost_analysis(df, funding_df, sym)

        # 3. Backtest with defaults
        backtest_convergence(df, sym, fit, funding_df)

        # 4. Parameter scan
        best = scan_parameters(df, sym, fit, funding_df)

    # Summary verdict
    print(f"\n{'='*70}")
    print("VERDICT")
    print(f"{'='*70}")
    print("""
  Calendar spread convergence in crypto:

  PROS:
  + Basis guaranteed -> 0 at expiry (structural edge)
  + Mean-reversion of richness is tradeable
  + Delta neutral (minimal directional risk)
  + No liquidation risk if properly margined

  CONS:
  - Funding cost on perp leg eats into profit
  - Quarterly futures have lower liquidity -> slippage
  - Capital locked in 2 positions (opportunity cost)
  - Edge is thin per trade (~0.03-0.08% net)

  KEY METRICS TO WATCH:
  - Annualized basis > funding APR -> trade is profitable
  - Richness > 1.5x expected -> entry signal
  - Quarterly volume > 10% of perp -> executable
    """)


if __name__ == "__main__":
    asyncio.run(main())
