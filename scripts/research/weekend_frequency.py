"""Weekend strategy: trade frequency analysis."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.validate_weekend_macro import load_macro_data, build_weekend_dataset
from scripts.research.weekend_mfe_trailing import (
    augment_dataset_with_missing_predictors,
    load_btc_1h,
    simulate_weekend_trades,
)

DATA = ROOT / "data" / "processed" / "candles"
PREDICTORS = ["china_inet_fri", "japan_fri", "tech_week", "energy_week", "usdjpy_week"]
MAJORITY = 3


def main():
    btc_4h = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    btc_4h["ts"] = pd.to_datetime(btc_4h["ts"], utc=True)
    btc_4h = btc_4h.set_index("ts").sort_index()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)
    ds = augment_dataset_with_missing_predictors(ds)
    btc_1h = load_btc_1h()
    df = simulate_weekend_trades(btc_1h, ds)

    total_weekends = len(ds)
    traded = len(df)
    print(f"Total weekends: {total_weekends}")
    print(f"Traded (majority >= 3): {traded} ({traded/total_weekends*100:.0f}%)")
    print(f"Skipped: {total_weekends - traded} ({(total_weekends-traded)/total_weekends*100:.0f}%)")

    # ── Per year ──
    df["year"] = df.date.dt.year
    ds_dates = pd.to_datetime(ds["fri_date"])

    print(f"\n{'='*60}")
    print("TRADES PER YEAR")
    print(f"{'='*60}\n")
    header = f"  {'Year':<6}{'Weekends':<10}{'Traded':<8}{'Rate':<8}{'Per month':<10}"
    print(header)
    print(f"  {'-'*45}")

    for year in sorted(set(ds_dates.dt.year)):
        yr_weekends = sum(ds_dates.dt.year == year)
        yr_trades = len(df[df.year == year])
        rate = yr_trades / yr_weekends * 100 if yr_weekends > 0 else 0
        per_month = yr_trades / 12 if year < 2026 else yr_trades / (5 + 1)  # through May
        print(f"  {year:<6}{yr_weekends:<10}{yr_trades:<8}{rate:<6.0f}%  {per_month:<6.1f}")

    # ── Consecutive streaks ──
    print(f"\n{'='*60}")
    print("CONSECUTIVE ENTRY STREAKS")
    print(f"{'='*60}\n")

    dates = df.date.sort_values().values
    gaps_days = np.diff(dates).astype("timedelta64[D]").astype(int)

    streak = 1
    streaks = []
    streak_start = dates[0]
    for i, gap in enumerate(gaps_days):
        if gap <= 8:
            streak += 1
        else:
            streaks.append((streak, streak_start, dates[i]))
            streak = 1
            streak_start = dates[i + 1]
    streaks.append((streak, streak_start, dates[-1]))

    streaks.sort(key=lambda x: -x[0])
    print("  Longest consecutive entry streaks:")
    for i, (s, start, end) in enumerate(streaks[:10]):
        s_str = pd.Timestamp(start).strftime("%Y-%m-%d")
        e_str = pd.Timestamp(end).strftime("%Y-%m-%d")
        print(f"    #{i+1}: {s} weeks ({s_str} to {e_str})")

    # Gap distribution
    print(f"\n  Gaps between trades:")
    gap_weeks = gaps_days / 7
    for g in [1, 2, 3, 4]:
        count = int(sum((gap_weeks >= g - 0.5) & (gap_weeks < g + 0.5)))
        print(f"    {g} week: {count} ({count/len(gaps_days)*100:.0f}%)")
    longer = int(sum(gap_weeks >= 4.5))
    print(f"    5+ weeks: {longer} ({longer/len(gaps_days)*100:.0f}%)")

    # ── Vote distribution ──
    print(f"\n{'='*60}")
    print("VOTE DISTRIBUTION (all weekends)")
    print(f"{'='*60}\n")

    available = [c for c in PREDICTORS if c in ds.columns]
    vote_sums = []
    for _, row in ds.iterrows():
        v = 0
        n_valid = 0
        for c in available:
            val = row.get(c, np.nan)
            if pd.isna(val):
                continue
            n_valid += 1
            v += 1 if val > 0 else -1
        vote_sums.append(v)

    vote_sums = np.array(vote_sums)
    print("  Vote distribution:")
    for vs in [-5, -3, -1, 1, 3, 5]:
        count = int(sum(vote_sums == vs))
        flag = "TRADE" if abs(vs) >= 3 else "skip"
        print(f"    vote={vs:+d}: {count:>3} ({count/len(vote_sums)*100:>4.1f}%) [{flag}]")

    trade_rate = sum(abs(vote_sums) >= 3) / len(vote_sums)
    print(f"\n  Trade rate: {trade_rate*100:.0f}% of weekends")
    print(f"  Expected: ~{trade_rate * 52:.0f} trades/year")

    # ── 2026 detail ──
    print(f"\n{'='*60}")
    print("2026 TRADES")
    print(f"{'='*60}\n")
    recent = df[df.year == 2026].sort_values("date")
    for _, t in recent.iterrows():
        print(f"  {t.date.strftime('%Y-%m-%d')} {t.direction:>5} pnl={t.pnl:+.2f}%")

    # ── Is 3-week streak unusual? ──
    print(f"\n{'='*60}")
    print("IS 3-WEEK STREAK UNUSUAL?")
    print(f"{'='*60}\n")
    streaks_3plus = [s for s in streaks if s[0] >= 3]
    print(f"  Streaks of 3+ consecutive weeks: {len(streaks_3plus)}")
    for s, start, end in sorted(streaks_3plus, key=lambda x: -x[0]):
        s_str = pd.Timestamp(start).strftime("%Y-%m-%d")
        e_str = pd.Timestamp(end).strftime("%Y-%m-%d")
        print(f"    {s} weeks: {s_str} to {e_str}")
    print(f"\n  Conclusion: 3 weeks in a row happens "
          f"~{len(streaks_3plus)} times in {(ds_dates.max()-ds_dates.min()).days//365} years"
          f" — {'normal' if len(streaks_3plus) >= 5 else 'somewhat unusual'}")

    print(f"\n{'='*60}")
    print("DONE")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
