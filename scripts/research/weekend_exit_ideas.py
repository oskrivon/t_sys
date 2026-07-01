"""Weekend exit optimization: 3 ideas.

1. Later exit (Mon 04:00 vs Sun 23:00)
2. Momentum/volatility-based exit within weekend
3. Different exit timing for long vs short

All with walk-forward H1/H2 validation.
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
COST_PCT = 0.15


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

    MAX_H = 64  # extend to Mon 13:00 for late exit tests
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
        hourly_close = {}
        hourly_high = {}
        hourly_low = {}
        hourly_range = {}  # candle range as % of price

        for _, c in candles.iterrows():
            h = int(round((c.name - entry_ts).total_seconds() / 3600))
            if h < 0 or h >= MAX_H:
                continue
            if direction == "long":
                hourly_pnl[h] = (c["close"] - entry_price) / entry_price * 100
            else:
                hourly_pnl[h] = (entry_price - c["close"]) / entry_price * 100
            hourly_close[h] = c["close"]
            hourly_high[h] = c["high"]
            hourly_low[h] = c["low"]
            hourly_range[h] = (c["high"] - c["low"]) / c["close"] * 100

        trades.append({
            "date": entry_ts,
            "direction": direction,
            "consensus": consensus,
            "entry_price": entry_price,
            "hourly_pnl": hourly_pnl,
            "hourly_close": hourly_close,
            "hourly_high": hourly_high,
            "hourly_low": hourly_low,
            "hourly_range": hourly_range,
        })

    return trades


def pnl_at(trade, h_target):
    if not trade["hourly_pnl"]:
        return np.nan
    best_h = min(trade["hourly_pnl"].keys(), key=lambda h: abs(h - h_target))
    return trade["hourly_pnl"][best_h]


def stats(pnls, freq):
    pnls = np.array(pnls)
    n = len(pnls)
    if n < 3:
        return {"n": n, "wr": 0, "avg": 0, "total": 0, "sharpe": 0, "mdd": 0}
    wr = (pnls > 0).mean() * 100
    avg = pnls.mean()
    total = pnls.sum()
    sh = avg / pnls.std() * sqrt(freq) if pnls.std() > 0 else 0
    cum = np.cumsum(pnls)
    mdd = (cum - np.maximum.accumulate(cum)).min()
    return {"n": n, "wr": wr, "avg": avg, "total": total, "sharpe": sh, "mdd": mdd}


def print_stats(label, s):
    print(f"  {label:<50} n={s['n']:>3}  WR={s['wr']:>5.1f}%  "
          f"avg={s['avg']:>+.3f}%  total={s['total']:>+7.1f}%  "
          f"Sharpe={s['sharpe']:>+.2f}  MDD={s['mdd']:>+.2f}%")


def wf_validate(trades, strategy_fn, label, freq):
    """Walk-forward: H1 in-sample, H2 out-of-sample."""
    mid = len(trades) // 2
    h1, h2 = trades[:mid], trades[mid:]
    f1 = len(h1) / ((h1[-1]["date"] - h1[0]["date"]).total_seconds() / (365.25 * 86400))
    f2 = len(h2) / ((h2[-1]["date"] - h2[0]["date"]).total_seconds() / (365.25 * 86400))

    base_h1 = [pnl_at(t, 51) - COST_PCT for t in h1]
    base_h2 = [pnl_at(t, 51) - COST_PCT for t in h2]
    strat_h1 = [strategy_fn(t) - COST_PCT for t in h1]
    strat_h2 = [strategy_fn(t) - COST_PCT for t in h2]

    s_base_h1 = stats(base_h1, f1)
    s_base_h2 = stats(base_h2, f2)
    s_strat_h1 = stats(strat_h1, f1)
    s_strat_h2 = stats(strat_h2, f2)

    print(f"\n  Walk-forward for: {label}")
    print(f"  {'':>50} {'Sharpe':>8} {'Total':>10} {'MDD':>8}")
    print(f"  {'H1 Baseline':<50} {s_base_h1['sharpe']:>+7.2f} {s_base_h1['total']:>+9.1f}% {s_base_h1['mdd']:>+7.2f}%")
    print(f"  {'H1 Strategy':<50} {s_strat_h1['sharpe']:>+7.2f} {s_strat_h1['total']:>+9.1f}% {s_strat_h1['mdd']:>+7.2f}%")
    print(f"  {'H2 Baseline (OOS)':<50} {s_base_h2['sharpe']:>+7.2f} {s_base_h2['total']:>+9.1f}% {s_base_h2['mdd']:>+7.2f}%")
    print(f"  {'H2 Strategy (OOS)':<50} {s_strat_h2['sharpe']:>+7.2f} {s_strat_h2['total']:>+9.1f}% {s_strat_h2['mdd']:>+7.2f}%")
    delta = s_strat_h2["sharpe"] - s_base_h2["sharpe"]
    print(f"  OOS Sharpe delta: {delta:+.3f}")
    return delta


def main():
    print("=" * 75)
    print("WEEKEND EXIT OPTIMIZATION: 3 IDEAS")
    print("=" * 75)

    btc = load_btc_4h()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc, macro)
    ds = augment_dataset_with_missing_predictors(ds)

    trades = build_trades(btc, ds)
    n = len(trades)
    print(f"\nTotal trades: {n}")
    longs = [t for t in trades if t["direction"] == "long"]
    shorts = [t for t in trades if t["direction"] == "short"]
    print(f"Long: {len(longs)}, Short: {len(shorts)}")

    years = (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)
    freq = n / years

    # ================================================================
    # BASELINE
    # ================================================================
    print(f"\n{'='*75}")
    print("BASELINE: exit at h=51 (Sun ~23:00)")
    print(f"{'='*75}")
    base_pnls = [pnl_at(t, 51) - COST_PCT for t in trades]
    print_stats("All trades", stats(base_pnls, freq))
    base_long = [pnl_at(t, 51) - COST_PCT for t in longs]
    base_short = [pnl_at(t, 51) - COST_PCT for t in shorts]
    print_stats("Longs", stats(base_long, freq))
    print_stats("Shorts", stats(base_short, freq))

    # ================================================================
    # IDEA 1: LATER EXIT
    # ================================================================
    print(f"\n{'='*75}")
    print("IDEA 1: LATER EXIT (extend hold past Sun 23:00)")
    print(f"{'='*75}")
    print(f"\n  {'Exit Hour':>10} {'Time':>14} {'Avg':>8} {'Total':>10} "
          f"{'Sharpe':>8} {'WR':>6} {'MDD':>8} {'vs Base':>10}")
    print(f"  {'-'*80}")

    base_total = sum(base_pnls)
    exit_hours_to_test = [35, 39, 43, 47, 51, 55, 59, 63]

    for eh in exit_hours_to_test:
        pnls = [pnl_at(t, eh) - COST_PCT for t in trades]
        s = stats(pnls, freq)
        day_offset = eh // 24
        hour_of_day = (21 + eh) % 24
        days = ["Fri", "Sat", "Sat", "Sun", "Sun", "Mon", "Mon"]
        day = days[min(day_offset, 6)]
        marker = " <<<" if eh == 51 else ""
        delta = s["total"] - base_total
        print(f"  h={eh:>3}  {day:>4} {hour_of_day:02d}:00  {s['avg']:>+7.3f}% "
              f"{s['total']:>+9.1f}% {s['sharpe']:>+7.2f} {s['wr']:>5.1f}% "
              f"{s['mdd']:>+7.2f}% {delta:>+9.1f}%{marker}")

    # By direction
    print(f"\n  LONGS:")
    print(f"  {'Exit Hour':>10} {'Avg':>8} {'Total':>10} {'Sharpe':>8}")
    for eh in [47, 51, 55, 59]:
        pnls = [pnl_at(t, eh) - COST_PCT for t in longs]
        s = stats(pnls, freq)
        print(f"  h={eh:>3}       {s['avg']:>+7.3f}% {s['total']:>+9.1f}% {s['sharpe']:>+7.2f}")

    print(f"\n  SHORTS:")
    for eh in [47, 51, 55, 59]:
        pnls = [pnl_at(t, eh) - COST_PCT for t in shorts]
        s = stats(pnls, freq)
        print(f"  h={eh:>3}       {s['avg']:>+7.3f}% {s['total']:>+9.1f}% {s['sharpe']:>+7.2f}")

    # WF for best later exit
    print()
    for eh in [55, 59]:
        wf_validate(trades, lambda t, _eh=eh: pnl_at(t, _eh),
                    f"Exit at h={eh}", freq)

    # ================================================================
    # IDEA 2: MOMENTUM/VOLATILITY CONDITIONAL EXIT
    # ================================================================
    print(f"\n{'='*75}")
    print("IDEA 2: MOMENTUM/VOLATILITY-BASED EARLY EXIT")
    print(f"{'='*75}")

    # 2A: Momentum reversal — if PnL was positive but turned negative
    print(f"\n  --- 2A: Momentum reversal (was up, now down) ---")
    print(f"  Check at each 4h step: if PnL was > +X% at any prior candle")
    print(f"  but now < 0%, exit immediately.")
    print()

    for peak_thresh in [0.3, 0.5, 0.75, 1.0, 1.5]:
        pnls = []
        n_early = 0
        for t in trades:
            sorted_hours = sorted(t["hourly_pnl"].keys())
            peak_seen = False
            exited_early = False
            for h in sorted_hours:
                if h > 52:
                    break
                p = t["hourly_pnl"][h]
                if p >= peak_thresh:
                    peak_seen = True
                if peak_seen and p < 0:
                    pnls.append(p - COST_PCT)
                    exited_early = True
                    n_early += 1
                    break
            if not exited_early:
                pnls.append(pnl_at(t, 51) - COST_PCT)

        s = stats(pnls, freq)
        delta = s["total"] - base_total
        print(f"  Peak>{peak_thresh:.1f}% then <0%: "
              f"n_early={n_early:>3}  Sharpe={s['sharpe']:>+.2f}  "
              f"total={s['total']:>+7.1f}%  delta={delta:>+7.1f}%")

    # 2B: Volatility expansion — if 4h candle range > X%, exit
    print(f"\n  --- 2B: Volatility spike exit ---")
    print(f"  If any 4h candle range > X% AND we're losing, exit.")
    print()

    for vol_thresh in [2.0, 3.0, 4.0, 5.0]:
        pnls = []
        n_early = 0
        for t in trades:
            sorted_hours = sorted(t["hourly_pnl"].keys())
            exited_early = False
            for h in sorted_hours:
                if h > 52 or h < 4:
                    continue
                rng = t["hourly_range"].get(h, 0)
                p = t["hourly_pnl"][h]
                if rng >= vol_thresh and p < 0:
                    pnls.append(p - COST_PCT)
                    exited_early = True
                    n_early += 1
                    break
            if not exited_early:
                pnls.append(pnl_at(t, 51) - COST_PCT)

        s = stats(pnls, freq)
        delta = s["total"] - base_total
        print(f"  Range>{vol_thresh:.0f}% + losing: "
              f"n_early={n_early:>3}  Sharpe={s['sharpe']:>+.2f}  "
              f"total={s['total']:>+7.1f}%  delta={delta:>+7.1f}%")

    # 2C: Consecutive negative candles — momentum death
    print(f"\n  --- 2C: Consecutive adverse candles ---")
    print(f"  If N consecutive 4h candles move against us, exit.")
    print()

    for n_consec in [2, 3, 4]:
        pnls = []
        n_early = 0
        for t in trades:
            sorted_hours = sorted(t["hourly_pnl"].keys())
            exited_early = False
            adverse_streak = 0
            prev_pnl = 0

            for h in sorted_hours:
                if h > 52 or h < 4:
                    continue
                p = t["hourly_pnl"][h]
                if p < prev_pnl:  # moved against us vs last candle
                    adverse_streak += 1
                else:
                    adverse_streak = 0

                if adverse_streak >= n_consec and p < 0:
                    pnls.append(p - COST_PCT)
                    exited_early = True
                    n_early += 1
                    break
                prev_pnl = p

            if not exited_early:
                pnls.append(pnl_at(t, 51) - COST_PCT)

        s = stats(pnls, freq)
        delta = s["total"] - base_total
        print(f"  {n_consec} adverse candles + losing: "
              f"n_early={n_early:>3}  Sharpe={s['sharpe']:>+.2f}  "
              f"total={s['total']:>+7.1f}%  delta={delta:>+7.1f}%")

    # 2D: Trailing stop from MFE (lock in profit after peak)
    print(f"\n  --- 2D: Trailing stop from peak ---")
    print(f"  After MFE reaches +X%, set trailing stop at peak - Y%.")
    print()

    for peak_req, trail in [(0.5, 0.3), (0.75, 0.5), (1.0, 0.5), (1.0, 0.75),
                             (1.5, 0.75), (1.5, 1.0), (2.0, 1.0), (2.0, 1.5)]:
        pnls = []
        n_early = 0
        for t in trades:
            sorted_hours = sorted(t["hourly_pnl"].keys())
            running_peak = 0.0
            trailing_active = False
            exited_early = False

            for h in sorted_hours:
                if h > 52 or h < 1:
                    continue
                p = t["hourly_pnl"][h]
                if p > running_peak:
                    running_peak = p
                if running_peak >= peak_req:
                    trailing_active = True
                if trailing_active and p <= running_peak - trail:
                    pnls.append(p - COST_PCT)
                    exited_early = True
                    n_early += 1
                    break

            if not exited_early:
                pnls.append(pnl_at(t, 51) - COST_PCT)

        s = stats(pnls, freq)
        delta = s["total"] - base_total
        print(f"  Peak>{peak_req:.1f}% trail={trail:.1f}%: "
              f"n_early={n_early:>3}  Sharpe={s['sharpe']:>+.2f}  "
              f"total={s['total']:>+7.1f}%  delta={delta:>+7.1f}%")

    # WF for best momentum ideas
    print()

    # Best from 2A: peak>1.0% then <0%
    def strat_momentum_reversal(t, peak_thresh=1.0):
        sorted_hours = sorted(t["hourly_pnl"].keys())
        peak_seen = False
        for h in sorted_hours:
            if h > 52:
                break
            p = t["hourly_pnl"][h]
            if p >= peak_thresh:
                peak_seen = True
            if peak_seen and p < 0:
                return p
        return pnl_at(t, 51)

    wf_validate(trades, strat_momentum_reversal,
                "Momentum reversal (peak>1.0% then <0%)", freq)

    # ================================================================
    # IDEA 3: DIFFERENT EXIT FOR LONG vs SHORT
    # ================================================================
    print(f"\n{'='*75}")
    print("IDEA 3: DIFFERENT EXIT TIMING FOR LONG vs SHORT")
    print(f"{'='*75}")

    # Full scan
    print(f"\n  Scan: exit longs at h_L, shorts at h_S")
    print(f"\n  {'h_L':>4} {'h_S':>4} {'Sharpe':>8} {'Total':>10} "
          f"{'WR':>6} {'MDD':>8} {'vs Base':>10}")
    print(f"  {'-'*55}")

    best_combo_sharpe = -999
    best_hl = 51
    best_hs = 51

    for hl in [39, 43, 47, 51, 55, 59]:
        for hs in [39, 43, 47, 51, 55]:
            pnls = []
            for t in trades:
                exit_h = hl if t["direction"] == "long" else hs
                pnls.append(pnl_at(t, exit_h) - COST_PCT)
            s = stats(pnls, freq)
            delta = s["total"] - base_total

            marker = ""
            if s["sharpe"] > best_combo_sharpe:
                best_combo_sharpe = s["sharpe"]
                best_hl = hl
                best_hs = hs
                marker = " ***"

            if hl == 51 and hs == 51:
                marker = " <<<"

            print(f"  L={hl:>3} S={hs:>3} {s['sharpe']:>+7.2f} {s['total']:>+9.1f}% "
                  f"{s['wr']:>5.1f}% {s['mdd']:>+7.2f}% {delta:>+9.1f}%{marker}")

    print(f"\n  >> Best: L=h{best_hl}, S=h{best_hs} (Sharpe {best_combo_sharpe:+.2f})")

    # WF validate best combo
    def strat_diff_exit(t, _hl=best_hl, _hs=best_hs):
        eh = _hl if t["direction"] == "long" else _hs
        return pnl_at(t, eh)

    wf_validate(trades, strat_diff_exit,
                f"L=h{best_hl} S=h{best_hs}", freq)

    # Also test the simple "longs hold longer" hypothesis
    print(f"\n  --- Hypothesis: longs benefit from holding longer ---")
    for hl in [55, 59]:
        def _strat(t, _hl=hl):
            eh = _hl if t["direction"] == "long" else 51
            return pnl_at(t, eh)
        wf_validate(trades, _strat, f"L=h{hl} S=h51", freq)

    # ================================================================
    # SUMMARY
    # ================================================================
    print(f"\n{'='*75}")
    print("SUMMARY OF ALL IDEAS")
    print(f"{'='*75}")
    print(f"\n  {'Idea':<55} {'Full Sharpe':>12} {'OOS verdict':>15}")
    print(f"  {'-'*82}")

    # Rerun WF for all and collect deltas
    ideas = []

    # Idea 1: later exit
    for eh in [55, 59]:
        d = wf_validate(trades, lambda t, _eh=eh: pnl_at(t, _eh),
                        f"1) Exit h={eh}", freq)
        pnls = [pnl_at(t, eh) - COST_PCT for t in trades]
        s = stats(pnls, freq)
        ideas.append((f"1) Exit at h={eh}", s["sharpe"], d))

    # Idea 2: momentum reversal
    d = wf_validate(trades, strat_momentum_reversal,
                    "2) Momentum reversal", freq)
    pnls = [strat_momentum_reversal(t) - COST_PCT for t in trades]
    s = stats(pnls, freq)
    ideas.append(("2) Momentum reversal (peak>1% then <0%)", s["sharpe"], d))

    # Idea 3: diff exit
    d = wf_validate(trades, strat_diff_exit,
                    f"3) L=h{best_hl} S=h{best_hs}", freq)
    pnls = [strat_diff_exit(t) - COST_PCT for t in trades]
    s = stats(pnls, freq)
    ideas.append((f"3) L=h{best_hl} S=h{best_hs}", s["sharpe"], d))

    print(f"\n  {'Idea':<55} {'Full Sharpe':>12} {'OOS delta':>12}")
    print(f"  {'-'*80}")
    print(f"  {'Baseline (h=51)':<55} {stats(base_pnls, freq)['sharpe']:>+11.2f} {'---':>12}")
    for name, sh, oos_d in ideas:
        verdict = "BETTER" if oos_d > 0.05 else ("WORSE" if oos_d < -0.05 else "NEUTRAL")
        print(f"  {name:<55} {sh:>+11.2f} {oos_d:>+11.3f}  {verdict}")

    print(f"\n{'='*75}")
    print("DONE")
    print(f"{'='*75}")


if __name__ == "__main__":
    main()
