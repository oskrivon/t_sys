"""
Astro-BTC Backtest: Mercury Retrograde + Lunar Phases vs BTC Returns

Hypothesis: retail traders influenced by astrology create measurable
patterns in crypto markets during Mercury Retrograde and lunar phases.

Tests:
1. BTC returns during Mercury Retrograde vs Direct periods
2. BTC returns around Full Moon vs New Moon
3. Volatility differences across astro periods
4. Combined signal analysis
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import ephem
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Tuple
import ccxt.async_support as ccxt


# --Mercury Retrograde dates 2017-2026 ------------------------------
# Source: astronomical data (well-documented recurring event)
# Format: (start_retrograde, end_retrograde)
MERCURY_RETROGRADE_PERIODS = [
    # 2017
    ("2017-01-01", "2017-01-08"),  # tail of 2016 retrograde
    ("2017-04-09", "2017-05-03"),
    ("2017-08-13", "2017-09-05"),
    ("2017-12-03", "2017-12-23"),
    # 2018
    ("2018-03-23", "2018-04-15"),
    ("2018-07-26", "2018-08-19"),
    ("2018-11-17", "2018-12-06"),
    # 2019
    ("2019-03-05", "2019-03-28"),
    ("2019-07-07", "2019-08-01"),
    ("2019-10-31", "2019-11-20"),
    # 2020
    ("2020-02-17", "2020-03-10"),
    ("2020-06-18", "2020-07-12"),
    ("2020-10-14", "2020-11-03"),
    # 2021
    ("2021-01-30", "2021-02-21"),
    ("2021-05-29", "2021-06-22"),
    ("2021-09-27", "2021-10-18"),
    # 2022
    ("2022-01-14", "2022-02-04"),
    ("2022-05-10", "2022-06-03"),
    ("2022-09-10", "2022-10-02"),
    ("2022-12-29", "2023-01-18"),
    # 2023
    ("2023-04-21", "2023-05-15"),
    ("2023-08-23", "2023-09-15"),
    ("2023-12-13", "2024-01-02"),
    # 2024
    ("2024-04-01", "2024-04-25"),
    ("2024-08-05", "2024-08-28"),
    ("2024-11-26", "2024-12-15"),
    # 2025
    ("2025-03-15", "2025-04-07"),
    ("2025-07-18", "2025-08-11"),
    ("2025-11-09", "2025-11-29"),
    # 2026
    ("2026-02-26", "2026-03-20"),
    ("2026-06-30", "2026-07-24"),
    ("2026-10-24", "2026-11-13"),
]


def get_lunar_phases(start_date: str, end_date: str) -> pd.DataFrame:
    """Calculate all full moons and new moons in date range using ephem."""
    phases = []
    d = ephem.Date(start_date)
    end = ephem.Date(end_date)

    # Get all new moons
    while d < end:
        next_new = ephem.next_new_moon(d)
        if next_new < end:
            dt = ephem.Date(next_new).datetime()
            phases.append({"date": dt.strftime("%Y-%m-%d"), "phase": "new_moon"})
        d = next_new + 1  # skip ahead

    # Get all full moons
    d = ephem.Date(start_date)
    while d < end:
        next_full = ephem.next_full_moon(d)
        if next_full < end:
            dt = ephem.Date(next_full).datetime()
            phases.append({"date": dt.strftime("%Y-%m-%d"), "phase": "full_moon"})
        d = next_full + 1

    df = pd.DataFrame(phases)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def build_mercury_mask(dates: pd.DatetimeIndex) -> pd.Series:
    """Return boolean series: True if date falls in Mercury Retrograde."""
    mask = pd.Series(False, index=dates)
    for start, end in MERCURY_RETROGRADE_PERIODS:
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        mask |= (dates >= s) & (dates <= e)
    return mask


async def fetch_btc_daily(since_year: int = 2017) -> pd.DataFrame:
    """Fetch BTC/USDT daily candles from Binance."""
    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        since = exchange.parse8601(f"{since_year}-01-01T00:00:00Z")
        all_candles = []
        while True:
            candles = await exchange.fetch_ohlcv(
                "BTC/USDT", "1d", since=since, limit=1000
            )
            if not candles:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + 86400000  # next day
            if len(candles) < 1000:
                break

        df = pd.DataFrame(
            all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("date")
        df["returns"] = df["close"].pct_change()
        df["log_returns"] = np.log(df["close"] / df["close"].shift(1))
        return df
    finally:
        await exchange.close()


def analyze_mercury_retrograde(df: pd.DataFrame) -> dict:
    """Compare BTC performance during Mercury Retrograde vs Direct."""
    df = df.copy()
    df["mercury_retro"] = build_mercury_mask(df.index)

    retro = df[df["mercury_retro"]]
    direct = df[~df["mercury_retro"]]

    # Per-period returns (buy at start, sell at end of retrograde)
    period_returns = []
    for start, end in MERCURY_RETROGRADE_PERIODS:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        period = df[(df.index >= s) & (df.index <= e)]
        if len(period) >= 2:
            ret = (period["close"].iloc[-1] / period["close"].iloc[0]) - 1
            period_returns.append({
                "start": start, "end": end,
                "return": ret,
                "days": len(period),
                "volatility": period["returns"].std() * np.sqrt(365),
            })

    # Same-length random periods for comparison
    direct_only = df[~df["mercury_retro"]].copy()
    avg_retro_days = int(np.mean([p["days"] for p in period_returns]))

    results = {
        "retrograde": {
            "n_days": len(retro),
            "mean_daily_return": retro["returns"].mean(),
            "median_daily_return": retro["returns"].median(),
            "std_daily": retro["returns"].std(),
            "annualized_vol": retro["returns"].std() * np.sqrt(365),
            "sharpe_approx": retro["returns"].mean() / retro["returns"].std() * np.sqrt(365) if retro["returns"].std() > 0 else 0,
            "positive_days_pct": (retro["returns"] > 0).mean(),
        },
        "direct": {
            "n_days": len(direct),
            "mean_daily_return": direct["returns"].mean(),
            "median_daily_return": direct["returns"].median(),
            "std_daily": direct["returns"].std(),
            "annualized_vol": direct["returns"].std() * np.sqrt(365),
            "sharpe_approx": direct["returns"].mean() / direct["returns"].std() * np.sqrt(365) if direct["returns"].std() > 0 else 0,
            "positive_days_pct": (direct["returns"] > 0).mean(),
        },
        "period_returns": period_returns,
    }

    return results


def analyze_lunar_phases(df: pd.DataFrame, lunar_df: pd.DataFrame, window: int = 3) -> dict:
    """Analyze BTC returns around full/new moons (±window days)."""
    results = {}

    for phase in ["full_moon", "new_moon"]:
        phase_dates = lunar_df[lunar_df["phase"] == phase]["date"]
        returns_around = []

        for d in phase_dates:
            mask = (df.index >= d - timedelta(days=window)) & (
                df.index <= d + timedelta(days=window)
            )
            period = df[mask]
            if len(period) >= 2:
                ret = (period["close"].iloc[-1] / period["close"].iloc[0]) - 1
                returns_around.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "return": ret,
                    "days": len(period),
                    "volatility": period["returns"].std(),
                })

        rets = [r["return"] for r in returns_around]
        results[phase] = {
            "n_events": len(returns_around),
            "mean_return": np.mean(rets) if rets else 0,
            "median_return": np.median(rets) if rets else 0,
            "positive_pct": np.mean([r > 0 for r in rets]) if rets else 0,
            "std_return": np.std(rets) if rets else 0,
            "best": max(rets) if rets else 0,
            "worst": min(rets) if rets else 0,
            "events": returns_around,
        }

    # Compare: full moon minus new moon
    fm_rets = [r["return"] for r in results["full_moon"]["events"]]
    nm_rets = [r["return"] for r in results["new_moon"]["events"]]

    # t-test
    from scipy import stats
    if len(fm_rets) > 2 and len(nm_rets) > 2:
        t_stat, p_value = stats.ttest_ind(fm_rets, nm_rets)
        results["comparison"] = {
            "t_statistic": t_stat,
            "p_value": p_value,
            "significant_5pct": p_value < 0.05,
        }

    return results


def analyze_combined_signals(df: pd.DataFrame, lunar_df: pd.DataFrame) -> dict:
    """Test combined astro signals."""
    df = df.copy()
    df["mercury_retro"] = build_mercury_mask(df.index)

    # Moon phase state: days since last new/full moon
    df["near_full_moon"] = False
    df["near_new_moon"] = False

    for _, row in lunar_df.iterrows():
        d = row["date"]
        mask = (df.index >= d - timedelta(days=2)) & (df.index <= d + timedelta(days=2))
        if row["phase"] == "full_moon":
            df.loc[mask, "near_full_moon"] = True
        else:
            df.loc[mask, "near_new_moon"] = True

    # Regime combinations
    regimes = {
        "retro + full_moon": df[df["mercury_retro"] & df["near_full_moon"]],
        "retro + new_moon": df[df["mercury_retro"] & df["near_new_moon"]],
        "retro (no moon event)": df[df["mercury_retro"] & ~df["near_full_moon"] & ~df["near_new_moon"]],
        "direct + full_moon": df[~df["mercury_retro"] & df["near_full_moon"]],
        "direct + new_moon": df[~df["mercury_retro"] & df["near_new_moon"]],
        "direct (no moon event)": df[~df["mercury_retro"] & ~df["near_full_moon"] & ~df["near_new_moon"]],
    }

    results = {}
    for name, subset in regimes.items():
        if len(subset) > 0:
            results[name] = {
                "n_days": len(subset),
                "mean_daily_return": subset["returns"].mean(),
                "annualized_vol": subset["returns"].std() * np.sqrt(365),
                "positive_days_pct": (subset["returns"] > 0).mean(),
            }

    return results


def yearly_breakdown(df: pd.DataFrame) -> dict:
    """Mercury retrograde returns by year."""
    df = df.copy()
    df["mercury_retro"] = build_mercury_mask(df.index)
    df["year"] = df.index.year

    results = {}
    for year in sorted(df["year"].unique()):
        yr = df[df["year"] == year]
        retro = yr[yr["mercury_retro"]]
        direct = yr[~yr["mercury_retro"]]
        if len(retro) > 5 and len(direct) > 5:
            results[year] = {
                "retro_mean": retro["returns"].mean(),
                "direct_mean": direct["returns"].mean(),
                "retro_better": retro["returns"].mean() > direct["returns"].mean(),
            }
    return results


def print_report(mercury: dict, lunar: dict, combined: dict, yearly: dict):
    """Print formatted analysis report."""
    print("=" * 70)
    print("  ASTRO-BTC BACKTEST: Mercury Retrograde + Lunar Phases")
    print("=" * 70)

    # Mercury Retrograde
    print("\n--Mercury Retrograde vs Direct --------------------------")
    for label in ["retrograde", "direct"]:
        d = mercury[label]
        print(f"\n  {label.upper()} ({d['n_days']} days):")
        print(f"    Mean daily return:  {d['mean_daily_return']*100:+.4f}%")
        print(f"    Median daily ret:   {d['median_daily_return']*100:+.4f}%")
        print(f"    Annualized vol:     {d['annualized_vol']*100:.1f}%")
        print(f"    Sharpe (approx):    {d['sharpe_approx']:.3f}")
        print(f"    Positive days:      {d['positive_days_pct']*100:.1f}%")

    # Period returns
    periods = mercury["period_returns"]
    wins = sum(1 for p in periods if p["return"] > 0)
    print(f"\n  Period-level (buy start -> sell end retrograde):")
    print(f"    Total periods:  {len(periods)}")
    print(f"    Positive:       {wins}/{len(periods)} ({wins/len(periods)*100:.0f}%)")
    print(f"    Mean return:    {np.mean([p['return'] for p in periods])*100:+.2f}%")
    print(f"    Median return:  {np.median([p['return'] for p in periods])*100:+.2f}%")

    print("\n    Top 5 retrograde periods:")
    sorted_p = sorted(periods, key=lambda x: x["return"], reverse=True)
    for p in sorted_p[:5]:
        print(f"      {p['start']} -- {p['end']}: {p['return']*100:+.1f}%")
    print("    Bottom 5:")
    for p in sorted_p[-5:]:
        print(f"      {p['start']} -- {p['end']}: {p['return']*100:+.1f}%")

    # Lunar Phases
    print("\n--Lunar Phases (±3 day window) --------------------------")
    for phase in ["full_moon", "new_moon"]:
        d = lunar[phase]
        print(f"\n  {phase.upper().replace('_', ' ')} ({d['n_events']} events):")
        print(f"    Mean return:    {d['mean_return']*100:+.3f}%")
        print(f"    Median return:  {d['median_return']*100:+.3f}%")
        print(f"    Positive:       {d['positive_pct']*100:.1f}%")
        print(f"    Best:           {d['best']*100:+.1f}%")
        print(f"    Worst:          {d['worst']*100:+.1f}%")

    if "comparison" in lunar:
        c = lunar["comparison"]
        sig = "YES" if c["significant_5pct"] else "NO"
        print(f"\n  T-test (full vs new moon): t={c['t_statistic']:.3f}, p={c['p_value']:.4f}")
        print(f"  Statistically significant (5%): {sig}")

    # Combined
    print("\n--Combined Signals --------------------------------------")
    for name, d in combined.items():
        print(f"  {name:30s}  n={d['n_days']:4d}  ret={d['mean_daily_return']*100:+.4f}%/d  "
              f"vol={d['annualized_vol']*100:.0f}%  pos={d['positive_days_pct']*100:.0f}%")

    # Yearly
    print("\n--Yearly Breakdown (Mercury Retro vs Direct) -----------")
    retro_wins = 0
    for year, d in yearly.items():
        marker = "<- RETRO" if d["retro_better"] else ""
        retro_wins += d["retro_better"]
        print(f"  {year}: retro={d['retro_mean']*100:+.4f}%/d  "
              f"direct={d['direct_mean']*100:+.4f}%/d  {marker}")
    print(f"\n  Retro outperformed in {retro_wins}/{len(yearly)} years")

    # Verdict
    print("\n--VERDICT ----------------------------------------------")
    retro_better = mercury["retrograde"]["mean_daily_return"] > mercury["direct"]["mean_daily_return"]
    moon_sig = lunar.get("comparison", {}).get("significant_5pct", False)
    print(f"  Mercury Retrograde effect: {'EXISTS (retro > direct)' if retro_better else 'NONE (direct >= retro)'}")
    print(f"  Lunar phase effect:        {'SIGNIFICANT' if moon_sig else 'NOT SIGNIFICANT'}")
    print(f"  Tradeable edge:            {'MAYBE — needs more analysis' if (retro_better or moon_sig) else 'UNLIKELY'}")
    print("=" * 70)


async def main():
    print("Fetching BTC/USDT daily data from Binance...")
    df = await fetch_btc_daily(2017)
    print(f"Loaded {len(df)} daily candles: {df.index[0].date()} to {df.index[-1].date()}")

    print("Calculating lunar phases...")
    lunar_df = get_lunar_phases(
        df.index[0].strftime("%Y/%m/%d"),
        df.index[-1].strftime("%Y/%m/%d"),
    )
    print(f"Found {len(lunar_df)} lunar events")

    print("Running analysis...\n")

    mercury = analyze_mercury_retrograde(df)
    lunar = analyze_lunar_phases(df, lunar_df, window=3)
    combined = analyze_combined_signals(df, lunar_df)
    yearly = yearly_breakdown(df)

    print_report(mercury, lunar, combined, yearly)

    # Save raw data for further analysis
    output_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'reports')
    os.makedirs(output_dir, exist_ok=True)

    # Save period returns
    pd.DataFrame(mercury["period_returns"]).to_csv(
        os.path.join(output_dir, "mercury_retrograde_periods.csv"), index=False
    )
    # Save lunar events
    for phase in ["full_moon", "new_moon"]:
        pd.DataFrame(lunar[phase]["events"]).to_csv(
            os.path.join(output_dir, f"lunar_{phase}_events.csv"), index=False
        )
    print(f"\nRaw data saved to {output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
