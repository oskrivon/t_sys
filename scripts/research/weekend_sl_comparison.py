"""Weekend SL comparison: No SL vs various SL levels at 3x leverage."""
from __future__ import annotations

import sys
from math import sqrt
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

LEVERAGE = 3
FEE_RT = 0.08  # roundtrip fee %


def stats(label, pnls_1x, freq, years):
    pnls_lev = pnls_1x * LEVERAGE - FEE_RT
    n = len(pnls_lev)
    wr = (pnls_lev > 0).mean() * 100
    avg = pnls_lev.mean()
    tot = pnls_lev.sum()
    sh = avg / pnls_lev.std() * sqrt(freq) if pnls_lev.std() > 0 else 0
    cum = np.cumsum(pnls_lev)
    mdd = (cum - np.maximum.accumulate(cum)).min()
    worst = pnls_lev.min()
    annual = tot / years
    print(f"  {label:<30} WR={wr:>5.1f}%  Avg={avg:>+6.2f}%  "
          f"Tot={tot:>+7.1f}%  Ann={annual:>+6.1f}%/yr  "
          f"Sh={sh:>+5.2f}  MDD={mdd:>+6.1f}%  Worst={worst:>+6.2f}%")


def main():
    btc_4h = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    btc_4h["ts"] = pd.to_datetime(btc_4h["ts"], utc=True)
    btc_4h = btc_4h.set_index("ts").sort_index()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)
    ds = augment_dataset_with_missing_predictors(ds)

    btc_1h = load_btc_1h()
    df = simulate_weekend_trades(btc_1h, ds)

    years = (df.date.max() - df.date.min()).days / 365.25
    freq = len(df) / years
    pnls_raw = df.pnl.values

    print(f"Total trades: {len(df)}, Years: {years:.1f}, Leverage: {LEVERAGE}x")

    # ── Main comparison ──
    print(f"\n{'='*95}")
    print(f"NO SL vs SL LEVELS (x{LEVERAGE} leverage, {FEE_RT}% fee/trade)")
    print(f"{'='*95}\n")

    stats("No SL (hold to Sunday)", pnls_raw, freq, years)
    print()
    for sl in [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
        pnls_sl = np.where(df.mae.values >= sl, -sl, pnls_raw)
        stats(f"SL {sl}%", pnls_sl, freq, years)

    # ── Loss distribution ──
    print(f"\n{'='*95}")
    print("LOSS DISTRIBUTION (1x, before leverage)")
    print(f"{'='*95}\n")
    losses = df[df.pnl < 0].pnl.values
    print(f"  Losing trades: {len(losses)} / {len(df)} ({len(losses)/len(df)*100:.0f}%)")
    print(f"  Loss percentiles:")
    for p in [50, 75, 90, 95, 99, 100]:
        val = np.percentile(losses, p)
        print(f"    P{p:>3}: {val:+.2f}% (x{LEVERAGE} = {val*LEVERAGE:+.2f}%)")

    # ── Net effect ──
    print(f"\n{'='*95}")
    print("NET EFFECT: damage to winners vs protection from losers")
    print(f"{'='*95}\n")
    hdr = f"  {'SL':<6} {'Win damage':<14} {'Loss protect':<14} {'Net 1x':<10} {'Net 3x':<10} {'Verdict':<8}"
    print(hdr)
    print(f"  {'-'*65}")
    for sl in [0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
        stopped = df[df.mae >= sl]
        winners_stopped = stopped[stopped.pnl > 0]
        # Winner damage: was +X, now forced to -SL
        winner_damage = (winners_stopped.pnl + sl).sum()

        # Loser protection: was -Y (Y > SL), now capped at -SL
        losers_deep = stopped[(stopped.pnl < 0) & (stopped.pnl < -sl)]
        loser_protection = (-losers_deep.pnl - sl).sum()

        net_1x = loser_protection - winner_damage
        net_3x = net_1x * LEVERAGE
        verdict = "HELPS" if net_1x > 0 else "HURTS"
        print(f"  {sl:<6} {winner_damage:>+10.1f}%   {loser_protection:>+10.1f}%   "
              f"{net_1x:>+7.1f}%   {net_3x:>+7.1f}%   {verdict}")

    # ── Equity curve checkpoints ──
    print(f"\n{'='*95}")
    print(f"EQUITY CURVE (cumulative PnL, {LEVERAGE}x)")
    print(f"{'='*95}\n")
    cum_nosl = np.cumsum(pnls_raw * LEVERAGE - FEE_RT)
    cum_sl075 = np.cumsum(np.where(df.mae.values >= 0.75, -0.75, pnls_raw) * LEVERAGE - FEE_RT)
    cum_sl2 = np.cumsum(np.where(df.mae.values >= 2.0, -2.0, pnls_raw) * LEVERAGE - FEE_RT)
    cum_sl3 = np.cumsum(np.where(df.mae.values >= 3.0, -3.0, pnls_raw) * LEVERAGE - FEE_RT)

    print(f"  {'Trade#':<8} {'No SL':<12} {'SL 0.75%':<12} {'SL 2%':<12} {'SL 3%':<12}")
    print(f"  {'-'*55}")
    for cp in [10, 25, 50, 75, 100, len(df) - 1]:
        if cp < len(df):
            print(f"  T={cp:<5} {cum_nosl[cp]:>+8.1f}%   {cum_sl075[cp]:>+8.1f}%   "
                  f"{cum_sl2[cp]:>+8.1f}%   {cum_sl3[cp]:>+8.1f}%")

    # ── Worst drawdown sequences ──
    print(f"\n{'='*95}")
    print("WORST CONSECUTIVE LOSSES (no SL, 1x)")
    print(f"{'='*95}\n")
    losing_streaks = []
    streak = 0
    streak_sum = 0
    for p in pnls_raw:
        if p < 0:
            streak += 1
            streak_sum += p
        else:
            if streak > 0:
                losing_streaks.append((streak, streak_sum))
            streak = 0
            streak_sum = 0
    if streak > 0:
        losing_streaks.append((streak, streak_sum))

    losing_streaks.sort(key=lambda x: x[1])
    print(f"  Top 5 worst streaks:")
    for i, (s, total) in enumerate(losing_streaks[:5]):
        print(f"    #{i+1}: {s} losses in a row, total {total:+.2f}% (x{LEVERAGE} = {total*LEVERAGE:+.2f}%)")

    # ── Per-year comparison ──
    print(f"\n{'='*95}")
    print(f"ANNUAL: No SL vs SL 2% (x{LEVERAGE})")
    print(f"{'='*95}\n")
    df["year"] = df.date.dt.year
    print(f"  {'Year':<6} {'N':<5} {'No SL tot':<12} {'SL 2% tot':<12} {'No SL worst':<14} {'SL 2% worst':<12}")
    print(f"  {'-'*65}")
    for year in sorted(df.year.unique()):
        yr = df[df.year == year]
        yr_raw = yr.pnl.values
        yr_sl2 = np.where(yr.mae.values >= 2.0, -2.0, yr_raw)
        nosl_tot = (yr_raw * LEVERAGE - FEE_RT).sum()
        sl2_tot = (yr_sl2 * LEVERAGE - FEE_RT).sum()
        nosl_worst = (yr_raw * LEVERAGE).min()
        sl2_worst = (yr_sl2 * LEVERAGE).min()
        print(f"  {year:<6} {len(yr):<5} {nosl_tot:>+9.1f}%   {sl2_tot:>+9.1f}%   "
              f"{nosl_worst:>+10.2f}%   {sl2_worst:>+8.2f}%")

    print(f"\n{'='*95}")
    print("DONE")
    print(f"{'='*95}")


if __name__ == "__main__":
    main()
