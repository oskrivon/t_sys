"""Weekend conditional exit: exit early if Saturday is unprofitable.

Based on finding from weekend_exit_timing.py:
  - Saturday UP (66% of trades): Sharpe ~2.2-2.9, strong edge
  - Saturday DOWN (34% of trades): Sharpe ~0, zero edge

Tests: if we detect Saturday in the red at hour X, exit immediately
instead of holding to Sunday 23:00. What's the combined result?
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.validate_weekend_macro import load_macro_data, build_weekend_dataset
from scripts.research.weekend_mfe_trailing import augment_dataset_with_missing_predictors

DATA = ROOT / "data" / "processed" / "candles"

PREDICTORS = ["china_inet_fri", "japan_fri", "tech_week", "energy_week", "usdjpy_week"]
MAJORITY = 3
COST_PCT = 0.15  # round-trip cost per trade


def load_btc_4h():
    df = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    if pd.api.types.is_numeric_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def get_signal(row, available):
    votes = 0
    n_valid = 0
    for c in available:
        val = row.get(c, np.nan)
        if pd.isna(val):
            continue
        n_valid += 1
        votes += 1 if val > 0 else -1
    if n_valid < MAJORITY or abs(votes) < MAJORITY:
        return None, 0
    return ("long" if votes > 0 else "short"), (n_valid + abs(votes)) // 2


def build_trades(btc, ds):
    available = [c for c in PREDICTORS if c in ds.columns]
    print(f"Available predictors: {available}")

    MAX_H = 60
    trades = []

    for _, row in ds.iterrows():
        direction, consensus = get_signal(row, available)
        if direction is None:
            continue

        fri_date = row.get("fri_date")
        entry_ts = pd.Timestamp(fri_date).replace(hour=21, minute=0)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")

        end_ts = entry_ts + pd.Timedelta(hours=MAX_H)
        candles = btc[(btc.index >= entry_ts) & (btc.index <= end_ts)]
        if len(candles) < 5:
            continue

        entry_price = candles.iloc[0]["close"]

        hourly_pnl = {}
        for _, c in candles.iterrows():
            h = int(round((c.name - entry_ts).total_seconds() / 3600))
            if h < 0 or h >= MAX_H:
                continue
            if direction == "long":
                hourly_pnl[h] = (c["close"] - entry_price) / entry_price * 100
            else:
                hourly_pnl[h] = (entry_price - c["close"]) / entry_price * 100

        trades.append({
            "date": entry_ts,
            "direction": direction,
            "consensus": consensus,
            "entry_price": entry_price,
            "hourly_pnl": hourly_pnl,
        })

    return trades


def pnl_at(trade, h_target):
    """Get PnL at closest available hour to h_target."""
    best_h = min(trade["hourly_pnl"].keys(), key=lambda h: abs(h - h_target))
    return trade["hourly_pnl"][best_h], best_h


def stats_line(label, pnls, freq):
    pnls = np.array(pnls)
    n = len(pnls)
    if n < 3:
        return f"  {label:<45} n={n} (too few)"
    wr = (pnls > 0).mean() * 100
    avg = pnls.mean()
    total = pnls.sum()
    sh = avg / pnls.std() * sqrt(freq) if pnls.std() > 0 else 0
    cum = np.cumsum(pnls)
    mdd = (cum - np.maximum.accumulate(cum)).min()
    return (f"  {label:<45} n={n:>3}  WR={wr:>5.1f}%  avg={avg:>+.3f}%  "
            f"total={total:>+7.1f}%  Sharpe={sh:>+.2f}  MDD={mdd:>+.2f}%")


def main():
    print("=" * 75)
    print("CONDITIONAL EXIT: EARLY OUT IF SATURDAY UNPROFITABLE")
    print("=" * 75)

    btc = load_btc_4h()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc, macro)
    ds = augment_dataset_with_missing_predictors(ds)

    trades = build_trades(btc, ds)
    print(f"\nTotal trades: {len(trades)}")
    if not trades:
        return

    years = (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)
    freq = len(trades) / years

    # Settlement hour (Sun ~23:00 on 4h grid = h=51)
    EXIT_H = 51

    # ── Section 1: Baseline ──
    print(f"\n{'='*75}")
    print("1. BASELINE: hold all trades to settlement (h=51)")
    print(f"{'='*75}")

    base_pnls = [pnl_at(t, EXIT_H)[0] - COST_PCT for t in trades]
    print(stats_line("Baseline (hold to Sun 23:00)", base_pnls, freq))

    # ── Section 2: Check-hour scan ──
    # At each Saturday hour, split trades into "up" vs "down",
    # test: exit "down" trades immediately vs hold to settlement
    print(f"\n{'='*75}")
    print("2. SCAN: at each Saturday check-hour, split UP/DOWN")
    print("   UP = hold to settlement, DOWN = exit at check-hour")
    print(f"{'='*75}")

    # Available 4h offsets on Saturday: h=3,7,11,15,19,23,27,31,35
    # Saturday starts at h=3 (Sat 00:00), meaningful check from h=7+
    check_hours = [7, 11, 15, 19, 23, 27, 31, 35]

    print(f"\n  {'Check':>6} {'Time':>12} {'#UP':>4} {'#DN':>4} "
          f"{'Combined Sharpe':>16} {'Combined Total':>15} {'vs Base':>10}")
    print(f"  {'-'*75}")

    best_combined_sharpe = -999
    best_check_h = None

    for ch in check_hours:
        day_offset = ch // 24
        hour_of_day = (21 + ch) % 24
        days = ["Fri", "Sat", "Sat", "Sun"]
        day = days[min(day_offset, 3)]
        time_label = f"{day} {hour_of_day:02d}:00"

        combined_pnls = []
        n_up = 0
        n_dn = 0

        for t in trades:
            check_pnl, _ = pnl_at(t, ch)
            exit_pnl, _ = pnl_at(t, EXIT_H)

            if check_pnl > 0:
                # Saturday UP: hold to settlement
                combined_pnls.append(exit_pnl - COST_PCT)
                n_up += 1
            else:
                # Saturday DOWN: exit now
                combined_pnls.append(check_pnl - COST_PCT)
                n_dn += 1

        combined = np.array(combined_pnls)
        avg = combined.mean()
        sh = avg / combined.std() * sqrt(freq) if combined.std() > 0 else 0
        total = combined.sum()
        base_total = sum(base_pnls)
        delta = total - base_total

        marker = ""
        if sh > best_combined_sharpe:
            best_combined_sharpe = sh
            best_check_h = ch
            marker = " ***"

        print(f"  h={ch:>3} {time_label:>12} {n_up:>4} {n_dn:>4} "
              f"{sh:>+15.2f} {total:>+14.1f}% {delta:>+9.1f}%{marker}")

    print(f"\n  >> Best check hour: h={best_check_h}")

    # ── Section 3: Deep dive on best check hour ──
    if best_check_h is None:
        return

    print(f"\n{'='*75}")
    print(f"3. DEEP DIVE: check at h={best_check_h}")
    print(f"{'='*75}")

    up_pnls_hold = []
    dn_pnls_hold = []
    dn_pnls_early = []
    up_trades_detail = []
    dn_trades_detail = []

    for t in trades:
        check_pnl, _ = pnl_at(t, best_check_h)
        exit_pnl, _ = pnl_at(t, EXIT_H)

        if check_pnl > 0:
            up_pnls_hold.append(exit_pnl - COST_PCT)
            up_trades_detail.append(t)
        else:
            dn_pnls_hold.append(exit_pnl - COST_PCT)
            dn_pnls_early.append(check_pnl - COST_PCT)
            dn_trades_detail.append(t)

    print(f"\n  Saturday UP group:")
    print(stats_line("  Hold to settlement", up_pnls_hold, freq))
    print(f"\n  Saturday DOWN group:")
    print(stats_line("  Hold to settlement (current)", dn_pnls_hold, freq))
    print(stats_line("  Exit at check hour (proposed)", dn_pnls_early, freq))

    # Compare combined strategies
    print(f"\n  Combined strategies:")
    print(stats_line("  A) Baseline (all hold)", base_pnls, freq))
    combined = up_pnls_hold + dn_pnls_early
    print(stats_line("  B) Conditional exit", combined, freq))

    # ── Section 4: What if "down" trades recover? ──
    print(f"\n{'='*75}")
    print(f"4. DO 'SATURDAY DOWN' TRADES RECOVER?")
    print(f"{'='*75}")

    dn_recovered = 0
    dn_worsened = 0
    dn_check_pnls = []
    dn_settle_pnls = []

    for t in dn_trades_detail:
        check_pnl, _ = pnl_at(t, best_check_h)
        exit_pnl, _ = pnl_at(t, EXIT_H)
        dn_check_pnls.append(check_pnl)
        dn_settle_pnls.append(exit_pnl)
        if exit_pnl > check_pnl:
            dn_recovered += 1
        else:
            dn_worsened += 1

    n_dn = len(dn_trades_detail)
    print(f"\n  Trades down at check: {n_dn}")
    print(f"  Recovered by settlement: {dn_recovered} ({dn_recovered/n_dn*100:.0f}%)")
    print(f"  Got worse: {dn_worsened} ({dn_worsened/n_dn*100:.0f}%)")
    print(f"  Avg PnL at check:      {np.mean(dn_check_pnls):+.3f}%")
    print(f"  Avg PnL at settlement: {np.mean(dn_settle_pnls):+.3f}%")
    print(f"  Avg improvement:       {np.mean(np.array(dn_settle_pnls) - np.array(dn_check_pnls)):+.3f}%")

    # Settled positive after being down at check?
    settled_pos = sum(1 for p in dn_settle_pnls if p > 0)
    print(f"  Settled positive: {settled_pos} ({settled_pos/n_dn*100:.0f}%)")

    # ── Section 5: Threshold scan ──
    # Maybe not just "down" vs "up" but a threshold
    print(f"\n{'='*75}")
    print(f"5. THRESHOLD SCAN: exit if PnL at check < threshold")
    print(f"{'='*75}")

    print(f"\n  {'Threshold':>10} {'#Exit':>6} {'#Hold':>6} "
          f"{'Sharpe':>8} {'Total':>10} {'vs Base':>10}")
    print(f"  {'-'*55}")

    base_total = sum(base_pnls)

    for thresh in [-2.0, -1.5, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5]:
        combined = []
        n_exit = 0
        for t in trades:
            check_pnl, _ = pnl_at(t, best_check_h)
            exit_pnl, _ = pnl_at(t, EXIT_H)
            if check_pnl < thresh:
                combined.append(check_pnl - COST_PCT)
                n_exit += 1
            else:
                combined.append(exit_pnl - COST_PCT)

        combined = np.array(combined)
        sh = combined.mean() / combined.std() * sqrt(freq) if combined.std() > 0 else 0
        total = combined.sum()
        delta = total - base_total
        print(f"  {thresh:>+9.2f}% {n_exit:>6} {len(trades)-n_exit:>6} "
              f"{sh:>+7.2f} {total:>+9.1f}% {delta:>+9.1f}%")

    # ── Section 6: Annual breakdown ──
    print(f"\n{'='*75}")
    print(f"6. ANNUAL BREAKDOWN: Baseline vs Conditional Exit")
    print(f"{'='*75}")

    print(f"\n  {'Year':>6} {'Base Total':>12} {'Cond Total':>12} {'Delta':>10} "
          f"{'#UP':>5} {'#DN':>5}")
    print(f"  {'-'*55}")

    for year in sorted(set(t["date"].year for t in trades)):
        yr_trades = [t for t in trades if t["date"].year == year]
        yr_base = []
        yr_cond = []
        yr_up = 0
        yr_dn = 0
        for t in yr_trades:
            check_pnl, _ = pnl_at(t, best_check_h)
            exit_pnl, _ = pnl_at(t, EXIT_H)
            yr_base.append(exit_pnl - COST_PCT)
            if check_pnl > 0:
                yr_cond.append(exit_pnl - COST_PCT)
                yr_up += 1
            else:
                yr_cond.append(check_pnl - COST_PCT)
                yr_dn += 1
        print(f"  {year:>6} {sum(yr_base):>+11.1f}% {sum(yr_cond):>+11.1f}% "
              f"{sum(yr_cond)-sum(yr_base):>+9.1f}% {yr_up:>5} {yr_dn:>5}")

    # ── Section 7: Walk-forward validation ──
    print(f"\n{'='*75}")
    print(f"7. WALK-FORWARD: train on H1, test on H2")
    print(f"{'='*75}")

    mid = len(trades) // 2
    h1 = trades[:mid]
    h2 = trades[mid:]
    freq_h1 = len(h1) / ((h1[-1]["date"] - h1[0]["date"]).total_seconds() / (365.25 * 86400))
    freq_h2 = len(h2) / ((h2[-1]["date"] - h2[0]["date"]).total_seconds() / (365.25 * 86400))

    for label, subset, f in [("H1 (in-sample)", h1, freq_h1), ("H2 (out-of-sample)", h2, freq_h2)]:
        base = [pnl_at(t, EXIT_H)[0] - COST_PCT for t in subset]
        cond = []
        for t in subset:
            check_pnl, _ = pnl_at(t, best_check_h)
            exit_pnl, _ = pnl_at(t, EXIT_H)
            if check_pnl > 0:
                cond.append(exit_pnl - COST_PCT)
            else:
                cond.append(check_pnl - COST_PCT)

        print(f"\n  {label} ({len(subset)} trades):")
        print(stats_line("    Baseline", base, f))
        print(stats_line("    Conditional exit", cond, f))

    print(f"\n{'='*75}")
    print("DONE")
    print(f"{'='*75}")


if __name__ == "__main__":
    main()
