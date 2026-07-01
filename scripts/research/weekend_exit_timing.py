"""Weekend exit timing optimization.

Answers: is Sunday 23:00 UTC the optimal exit time, or is there
a better hour? Analyzes hour-by-hour PnL profile within weekends
to find if profit peaks earlier and evaporates by settlement.

Outputs:
  1. Hourly PnL curve (avg unrealized PnL at each hour offset from entry)
  2. Optimal exit hour by Sharpe
  3. Long vs Short breakdown
  4. Take-profit analysis (exit when unrealized hits X%)
  5. Confidence-weighted exit (does 5/5 consensus behave differently?)
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
COST_PCT = 0.15  # round-trip cost


def load_btc_4h():
    """Use 4h candles — covers 2017-2026 (vs 1h only 6 months)."""
    df = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    if pd.api.types.is_numeric_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def get_signal(row, available):
    """Compute ensemble signal for a weekend row."""
    votes = 0
    n_valid = 0
    for c in available:
        val = row.get(c, np.nan)
        if pd.isna(val):
            continue
        n_valid += 1
        votes += 1 if val > 0 else -1

    if n_valid < MAJORITY or abs(votes) < MAJORITY:
        return None, 0, 0
    direction = "long" if votes > 0 else "short"
    consensus = (n_valid + abs(votes)) // 2
    return direction, consensus, n_valid


def build_hourly_profiles(btc, ds):
    """Build 4h-step unrealized PnL for each weekend trade.

    Uses 4h candles for full 5-year history. Each 4h candle maps to
    closest hour offset from entry (Fri 21:00). We interpolate to
    cover key exit points.
    """
    available = [c for c in PREDICTORS if c in ds.columns]
    print(f"Available predictors: {available}")

    # Max hours from Fri 21:00 to Mon 08:00 = ~59 hours
    MAX_HOURS = 60
    trades = []

    for _, row in ds.iterrows():
        direction, consensus, n_valid = get_signal(row, available)
        if direction is None:
            continue

        fri_date = row.get("fri_date")
        entry_ts = pd.Timestamp(fri_date).replace(hour=21, minute=0)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")

        # Get candles from entry to Mon 08:00 (extended window)
        end_ts = entry_ts + pd.Timedelta(hours=MAX_HOURS)
        mask = (btc.index >= entry_ts) & (btc.index <= end_ts)
        candles = btc[mask]

        if len(candles) < 5:
            continue

        entry_price = candles.iloc[0]["close"]

        # Compute unrealized PnL at each 4h step
        hourly_pnl = {}
        hourly_mfe = {}
        running_mfe = 0.0

        for _, c in candles.iterrows():
            h_offset = int(round((c.name - entry_ts).total_seconds() / 3600))
            if h_offset < 0 or h_offset >= MAX_HOURS:
                continue

            if direction == "long":
                pnl = (c["close"] - entry_price) / entry_price * 100
                high_pnl = (c["high"] - entry_price) / entry_price * 100
            else:
                pnl = (entry_price - c["close"]) / entry_price * 100
                high_pnl = (entry_price - c["low"]) / entry_price * 100

            running_mfe = max(running_mfe, high_pnl)
            hourly_pnl[h_offset] = pnl
            hourly_mfe[h_offset] = running_mfe

        # Standard exit PnL — find closest to h=50 (Sun 23:00)
        # 4h candles at 21:00 entry -> offsets 3,7,11,...,51
        # h=51 = Sun 00:00 next day, h=47 = Sun 20:00
        exit_pnl = np.nan
        for h_try in [51, 47, 55, 43]:
            if h_try in hourly_pnl:
                exit_pnl = hourly_pnl[h_try]
                break

        trades.append({
            "date": entry_ts,
            "direction": direction,
            "consensus": consensus,
            "n_valid": n_valid,
            "entry_price": entry_price,
            "exit_pnl": exit_pnl,
            "hourly_pnl": hourly_pnl,
            "hourly_mfe": hourly_mfe,
        })

    return trades


def analyze_optimal_exit_hour(trades, label="ALL"):
    """Find optimal exit hour by Sharpe."""
    MAX_HOURS = 60
    n = len(trades)
    years = (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)
    freq = n / years if years > 0 else 18  # trades per year

    print(f"\n{'='*70}")
    print(f"HOURLY PnL PROFILE — {label} ({n} trades)")
    print(f"{'='*70}")
    print(f"{'Hour':>6} {'Day':>5} {'Time':>6} {'Avg%':>8} {'Med%':>8} "
          f"{'WR%':>6} {'Sharpe':>7} {'Total%':>9} {'N':>4}")
    print(f"{'-'*70}")

    best_sharpe = -999
    best_hour = 51  # default: closest to Sun 23:00
    results = []

    # Collect all hour offsets that appear in the data
    all_offsets = sorted(set(h for t in trades for h in t["hourly_pnl"].keys()))

    for h in all_offsets:
        if h < 1 or h > 56:
            continue
        pnls = []
        for t in trades:
            p = t["hourly_pnl"].get(h)
            if p is not None:
                pnls.append(p - COST_PCT)
        if len(pnls) < 10:
            continue

        pnls = np.array(pnls)
        avg = pnls.mean()
        med = np.median(pnls)
        wr = (pnls > 0).mean() * 100
        sh = avg / pnls.std() * sqrt(freq) if pnls.std() > 0 else 0
        total = pnls.sum()

        # Day/time label
        day_offset = h // 24
        hour_of_day = (21 + h) % 24
        days = ["Fri", "Sat", "Sat", "Sun", "Sun", "Mon"]
        day_name = days[min(day_offset, 5)]

        results.append({
            "hour": h, "avg": avg, "med": med, "wr": wr,
            "sharpe": sh, "total": total, "n": len(pnls),
            "day": day_name, "time": f"{hour_of_day:02d}:00",
        })

        if sh > best_sharpe:
            best_sharpe = sh
            best_hour = h

    # Find which offset is closest to h=51 (our current exit)
    current_h = min(all_offsets, key=lambda x: abs(x - 51)) if all_offsets else 51

    for r in results:
        marker = " ***" if r["hour"] == best_hour else (" <<<" if r["hour"] == current_h else "")
        print(f"  h={r['hour']:>3} {r['day']:>5} {r['time']:>6} {r['avg']:>+7.3f}% "
              f"{r['med']:>+7.3f}% {r['wr']:>5.1f}% {r['sharpe']:>+6.2f} "
              f"{r['total']:>+8.1f}% {r['n']:>4}{marker}")

    print(f"\n  >> Best exit: h={best_hour} (Sharpe {best_sharpe:+.2f})")
    print(f"  >> Current:   h={current_h} ~ Sun 23:00")

    return results, best_hour


def analyze_take_profit(trades, label="ALL"):
    """Test take-profit exits: close when unrealized PnL hits threshold."""
    print(f"\n{'='*70}")
    print(f"TAKE-PROFIT ANALYSIS — {label}")
    print(f"{'='*70}")

    n = len(trades)
    years = (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)
    freq = n / years if years > 0 else 18

    print(f"{'TP Level':>10} {'Triggered':>10} {'Avg PnL':>10} {'Sharpe':>8} "
          f"{'Total%':>10} {'vs Base':>10}")
    print(f"{'-'*65}")

    # Baseline: hold to h=50
    base_pnls = np.array([t["exit_pnl"] - COST_PCT for t in trades
                          if not np.isnan(t["exit_pnl"])])
    base_total = base_pnls.sum()
    base_sharpe = base_pnls.mean() / base_pnls.std() * sqrt(freq) if base_pnls.std() > 0 else 0
    print(f"{'Baseline':>10} {'-':>10} {base_pnls.mean():>+9.3f}% {base_sharpe:>+7.2f} "
          f"{base_total:>+9.1f}% {'-':>10}")

    for tp_level in [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]:
        tp_pnls = []
        triggered_count = 0

        for t in trades:
            hit = False
            # Scan hour by hour: if MFE hits TP, take it at close of that hour
            for h in sorted(t["hourly_pnl"].keys()):
                if h > 52:
                    break
                mfe_at_h = t["hourly_mfe"].get(h, 0)
                if mfe_at_h >= tp_level:
                    tp_pnls.append(t["hourly_pnl"][h] - COST_PCT)
                    hit = True
                    triggered_count += 1
                    break
            if not hit:
                # Hold to settlement
                if not np.isnan(t["exit_pnl"]):
                    tp_pnls.append(t["exit_pnl"] - COST_PCT)

        tp_pnls = np.array(tp_pnls)
        avg = tp_pnls.mean()
        sh = avg / tp_pnls.std() * sqrt(freq) if tp_pnls.std() > 0 else 0
        total = tp_pnls.sum()
        delta = total - base_total
        print(f"{tp_level:>9.1f}% {triggered_count:>10} {avg:>+9.3f}% {sh:>+7.2f} "
              f"{total:>+9.1f}% {delta:>+9.1f}%")


def analyze_mfe_decay(trades, label="ALL"):
    """How much MFE is 'left on the table' at settlement?"""
    print(f"\n{'='*70}")
    print(f"MFE DECAY — {label}")
    print(f"{'='*70}")

    mfe_at_exit = []
    for t in trades:
        mfe_max = max(t["hourly_mfe"].values()) if t["hourly_mfe"] else 0
        exit_pnl = t["exit_pnl"] if not np.isnan(t["exit_pnl"]) else 0
        decay = mfe_max - exit_pnl
        mfe_at_exit.append({
            "date": t["date"],
            "direction": t["direction"],
            "mfe_max": mfe_max,
            "exit_pnl": exit_pnl,
            "decay": decay,
            "decay_frac": decay / mfe_max if mfe_max > 0 else 0,
        })

    df = pd.DataFrame(mfe_at_exit)
    print(f"\n  Avg MFE peak:     {df.mfe_max.mean():+.3f}%")
    print(f"  Avg exit PnL:     {df.exit_pnl.mean():+.3f}%")
    print(f"  Avg decay:        {df.decay.mean():+.3f}% ({df.decay_frac.mean()*100:.0f}% of MFE lost)")
    print(f"  Median decay:     {df.decay.median():+.3f}%")

    # Winners vs losers
    winners = df[df.exit_pnl > 0]
    losers = df[df.exit_pnl <= 0]
    if len(winners) > 0:
        print(f"\n  Winners ({len(winners)}): MFE peak {winners.mfe_max.mean():+.3f}%, "
              f"settled {winners.exit_pnl.mean():+.3f}%, "
              f"decay {winners.decay.mean():+.3f}% ({winners.decay_frac.mean()*100:.0f}%)")
    if len(losers) > 0:
        print(f"  Losers  ({len(losers)}): MFE peak {losers.mfe_max.mean():+.3f}%, "
              f"settled {losers.exit_pnl.mean():+.3f}%, "
              f"decay {losers.decay.mean():+.3f}% ({losers.decay_frac.mean()*100:.0f}%)")

    # When does MFE peak occur?
    print(f"\n  MFE peak hour distribution:")
    peak_hours = []
    for t in trades:
        if t["hourly_mfe"]:
            max_mfe = max(t["hourly_mfe"].values())
            peak_h = min(h for h, v in t["hourly_mfe"].items() if v == max_mfe)
            peak_hours.append(peak_h)
    peak_hours = np.array(peak_hours)
    for p in [10, 25, 50, 75, 90]:
        h = int(np.percentile(peak_hours, p))
        day_offset = h // 24
        hour_of_day = (21 + h) % 24
        days = ["Fri", "Sat", "Sat", "Sun", "Sun", "Mon"]
        day = days[min(day_offset, 5)]
        print(f"    P{p}: h={h} ({day} {hour_of_day:02d}:00)")


def analyze_by_consensus(trades):
    """Does 5/5 consensus behave differently from 3/5?"""
    print(f"\n{'='*70}")
    print(f"EXIT TIMING BY CONSENSUS STRENGTH")
    print(f"{'='*70}")

    for min_cons, max_cons, label in [(3, 3, "3/5"), (4, 5, "4-5/5")]:
        subset = [t for t in trades if min_cons <= t["consensus"] <= max_cons]
        if len(subset) < 10:
            print(f"\n  {label}: too few trades ({len(subset)})")
            continue
        analyze_optimal_exit_hour(subset, f"Consensus {label}")


def analyze_saturday_momentum(trades):
    """If Saturday is going well/badly, does exit timing change?"""
    print(f"\n{'='*70}")
    print(f"EXIT TIMING BY SATURDAY MOMENTUM")
    print(f"{'='*70}")

    # Split by PnL at ~h=27 (Sat 00:00, closest 4h grid to Saturday mid)
    def _sat_pnl(t):
        for h_try in [27, 23, 31]:
            if h_try in t["hourly_pnl"]:
                return t["hourly_pnl"][h_try]
        return 0
    sat_up = [t for t in trades if _sat_pnl(t) > 0]
    sat_dn = [t for t in trades if _sat_pnl(t) <= 0]

    if len(sat_up) >= 10:
        print(f"\n  Saturday profitable ({len(sat_up)} trades):")
        analyze_optimal_exit_hour(sat_up, "Sat UP")
    if len(sat_dn) >= 10:
        print(f"\n  Saturday unprofitable ({len(sat_dn)} trades):")
        analyze_optimal_exit_hour(sat_dn, "Sat DOWN")


def main():
    print("=" * 70)
    print("WEEKEND EXIT TIMING OPTIMIZATION")
    print("=" * 70)

    btc_4h = load_btc_4h()

    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)
    ds = augment_dataset_with_missing_predictors(ds)

    print(f"\nBTC 4h: {btc_4h.index[0]} -- {btc_4h.index[-1]} ({len(btc_4h)} candles)")
    print(f"Weekend dataset: {len(ds)} weekends\n")

    trades = build_hourly_profiles(btc_4h, ds)
    print(f"Total trades with signal: {len(trades)}")
    if not trades:
        print("NO TRADES")
        return

    longs = [t for t in trades if t["direction"] == "long"]
    shorts = [t for t in trades if t["direction"] == "short"]
    print(f"Direction: long={len(longs)}, short={len(shorts)}")

    # 1. Overall hourly profile
    analyze_optimal_exit_hour(trades, "ALL")

    # 2. By direction
    if len(longs) >= 10:
        analyze_optimal_exit_hour(longs, "LONG")
    if len(shorts) >= 10:
        analyze_optimal_exit_hour(shorts, "SHORT")

    # 3. Take-profit analysis
    analyze_take_profit(trades, "ALL")
    if len(longs) >= 10:
        analyze_take_profit(longs, "LONG")
    if len(shorts) >= 10:
        analyze_take_profit(shorts, "SHORT")

    # 4. MFE decay
    analyze_mfe_decay(trades, "ALL")

    # 5. By consensus
    analyze_by_consensus(trades)

    # 6. Saturday momentum conditional
    analyze_saturday_momentum(trades)

    print(f"\n{'='*70}")
    print("DONE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
