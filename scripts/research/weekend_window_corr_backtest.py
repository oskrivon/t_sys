"""Weekend: extended hold window (days) + BTC/SPY correlation gate.

Complements WEEKEND_EXIT_TIMING_RESEARCH (which tested exit by HOURS, up to
Mon 08:00). Here we test two NEW hypotheses raised from the live losing streak
(1 win / 7 since 2026-05, see paper_trades.db weekend_trades):

  H1 "extended window": the ensemble predicts a multi-DAY drift (3-5 days),
     but the trade is locked in the 48h weekend window. Does holding into the
     following week (Mon..Fri) recover the losers? With/without a -2% SL.

  H2 "correlation gate": the TradFi->BTC link is currently weak (corr20 ~0.4).
     Does gating entries on corr20(BTC,SPY) > threshold improve the edge?

Reuses the exact signal pipeline from validate_weekend_macro.
Run:  python scripts/research/weekend_window_corr_backtest.py
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.validate_weekend_macro import (
    load_btc_4h,
    load_macro_data,
    build_weekend_dataset,
)
from scripts.research.weekend_mfe_trailing import augment_dataset_with_missing_predictors

PREDICTORS = ["china_inet_fri", "japan_fri", "tech_week", "energy_week", "usdjpy_week"]
MAJORITY = 3
COST_PCT = 0.15      # round-trip cost in %
SL_PCT = 5.0         # production catastrophe stop (config.py stop_loss_pct=0.05)

# Exit horizons in hours from Fri 21:00 entry.
# Production exit = Sun 12:00 UTC (config exit_weekday=6, exit_hour_utc=12) = 39h.
HORIZONS = {
    "Sun12 (prod)": 39,
    "Sun23": 50,
    "+1d Mon": 63,
    "+2d Tue": 87,
    "+3d Wed": 111,
    "+5d Fri": 159,
}


def extend_btc_live(btc):
    """Append fresh 4h candles from binance so the backtest covers the live
    losing streak (parquet ends ~2026-05-12)."""
    try:
        import ccxt
    except ImportError:
        return btc
    ex = ccxt.binance({"options": {"defaultType": "future"}})
    last = btc.index[-1]
    since = int(last.timestamp() * 1000) + 1
    out = []
    while True:
        b = ex.fetch_ohlcv("BTC/USDT", "4h", since=since, limit=1000)
        if not b:
            break
        out += b
        since = b[-1][0] + 1
        if len(b) < 1000:
            break
    if not out:
        return btc
    add = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])
    add["ts"] = pd.to_datetime(add["ts"], unit="ms", utc=True)
    add = add.set_index("ts")
    add = add[[c for c in btc.columns if c in add.columns]]
    print(f"  Extended BTC with {len(add)} live 4h candles -> {add.index[-1]}")
    return pd.concat([btc, add]).sort_index()


def get_signal(row, rule="net"):
    """rule='net'   -> validated rule: |vote_sum| >= 3  (==4-of-5 agree)
       rule='count' -> LIVE rule: max(bull,bear) count >= 3 (==3-of-5 agree)"""
    votes, n = 0, 0
    for c in PREDICTORS:
        v = row.get(c, np.nan)
        if pd.isna(v):
            continue
        n += 1
        votes += 1 if v > 0 else -1
    if n < MAJORITY:
        return None
    if rule == "net":
        if abs(votes) < MAJORITY:
            return None
        return "long" if votes > 0 else "short"
    # count rule (production)
    bull = (n + votes) // 2
    bear = (n - votes) // 2
    if bull >= MAJORITY:
        return "long"
    if bear >= MAJORITY:
        return "short"
    return None


def price_at(btc, ts):
    sub = btc[btc.index <= ts]
    return float(sub.iloc[-1]["close"]) if len(sub) else None


def build_corr_series(btc, macro, window=20):
    """rolling corr of daily returns BTC vs SPY, indexed by date (tz-naive)."""
    spy = macro.get("sp500")
    if spy is None:
        return None
    btc_d = btc["close"].resample("1D").last().pct_change()
    btc_d.index = btc_d.index.tz_localize(None)
    spy_d = spy.set_index("date")["close"].sort_index().pct_change()
    df = pd.DataFrame({"btc": btc_d, "spy": spy_d}).dropna()
    return df["btc"].rolling(window).corr(df["spy"])


def corr_at(corr, fri_date):
    if corr is None:
        return np.nan
    d = pd.Timestamp(fri_date)
    if d.tzinfo:
        d = d.tz_localize(None)
    sub = corr[corr.index <= d].dropna()
    return float(sub.iloc[-1]) if len(sub) else np.nan


def build_trades(btc, ds, corr, rule="net"):
    trades = []
    for _, row in ds.iterrows():
        direction = get_signal(row, rule)
        if direction is None:
            continue
        fri_date = row.get("fri_date")
        entry_ts = pd.Timestamp(fri_date).replace(hour=21, minute=0)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")
        entry_price = price_at(btc, entry_ts)
        if not entry_price:
            continue
        sign = 1 if direction == "long" else -1

        # path window up to max horizon for SL detection
        max_h = max(HORIZONS.values())
        win = btc[(btc.index >= entry_ts) &
                  (btc.index <= entry_ts + pd.Timedelta(hours=max_h))]
        if len(win) < 5:
            continue

        # PnL at each horizon (no SL)
        pnl = {}
        for label, h in HORIZONS.items():
            p = price_at(btc, entry_ts + pd.Timedelta(hours=h))
            pnl[label] = sign * (p - entry_price) / entry_price * 100 if p else np.nan

        # PnL at each horizon WITH path-based -SL_PCT stop
        pnl_sl = {}
        for label, h in HORIZONS.items():
            seg = win[win.index <= entry_ts + pd.Timedelta(hours=h)]
            stopped = False
            for _, c in seg.iterrows():
                adverse = ((entry_price - c["low"]) / entry_price * 100 if sign > 0
                           else (c["high"] - entry_price) / entry_price * 100)
                if adverse >= SL_PCT:
                    pnl_sl[label] = -SL_PCT
                    stopped = True
                    break
            if not stopped:
                pnl_sl[label] = pnl[label]

        trades.append({
            "date": entry_ts,
            "direction": direction,
            "entry_price": entry_price,
            "pnl": pnl,
            "pnl_sl": pnl_sl,
            "corr": corr_at(corr, fri_date),
        })
    return trades


def stats(pnls, freq):
    a = np.array([p - COST_PCT for p in pnls if not np.isnan(p)])
    if len(a) < 5:
        return None
    sh = a.mean() / a.std() * sqrt(freq) if a.std() > 0 else 0
    return dict(n=len(a), wr=(a > 0).mean() * 100, avg=a.mean(),
               total=a.sum(), sharpe=sh)


def yrs(trades):
    return (trades[-1]["date"] - trades[0]["date"]).total_seconds() / (365.25 * 86400)


def report_window(trades, key, title):
    print(f"\n{'='*72}\n{title}\n{'='*72}")
    freq = len(trades) / max(yrs(trades), 0.1)
    print(f"{'Exit':>14} {'N':>4} {'WR%':>6} {'Avg%':>8} {'Total%':>9} {'Sharpe':>7}")
    print("-" * 56)
    for label in HORIZONS:
        s = stats([t[key][label] for t in trades], freq)
        if s:
            print(f"{label:>14} {s['n']:>4} {s['wr']:>5.0f} {s['avg']:>+7.3f} "
                  f"{s['total']:>+8.1f} {s['sharpe']:>+6.2f}")


def report_corr_gate(trades):
    print(f"\n{'='*72}\nH2: CORRELATION GATE (exit Sun12 prod, 5% SL)\n{'='*72}")
    print(f"corr20(BTC,SPY) coverage: "
          f"{sum(not np.isnan(t['corr']) for t in trades)}/{len(trades)} trades")
    print(f"\n{'Gate':>14} {'N':>4} {'WR%':>6} {'Avg%':>8} {'Total%':>9} {'Sharpe':>7}")
    print("-" * 56)
    base_label = "Sun12 (prod)"
    for thr in [-1.0, 0.0, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7]:
        sub = [t for t in trades if not np.isnan(t["corr"]) and t["corr"] >= thr]
        if len(sub) < 5:
            print(f"{'>= '+f'{thr:+.2f}':>14} {len(sub):>4}  (too few)")
            continue
        freq = len(sub) / max(yrs(sub), 0.1)
        s = stats([t["pnl_sl"][base_label] for t in sub], freq)
        tag = "  <-- proposed" if abs(thr - 0.55) < 1e-9 else ""
        if s:
            print(f"{'>= '+f'{thr:+.2f}':>14} {s['n']:>4} {s['wr']:>5.0f} "
                  f"{s['avg']:>+7.3f} {s['total']:>+8.1f} {s['sharpe']:>+6.2f}{tag}")


def report_subperiods(trades):
    print(f"\n{'='*72}\nSUBPERIODS (Sun12 prod exit, 5% SL)\n{'='*72}")
    print(f"{'Period':>14} {'N':>4} {'WR%':>6} {'Avg%':>8} {'Total%':>9} {'Sharpe':>7}")
    print("-" * 56)
    base = "Sun12 (prod)"
    for lo, hi, name in [(2021, 2024, "2021-2023"), (2024, 2026, "2024-2025"),
                         (2026, 2027, "2026 (live)")]:
        sub = [t for t in trades if lo <= t["date"].year < hi]
        if len(sub) < 5:
            print(f"{name:>14} {len(sub):>4}  (too few)")
            continue
        freq = len(sub) / max(yrs(sub), 0.1)
        s = stats([t[base][base] if False else t["pnl_sl"][base] for t in sub], freq)
        if s:
            print(f"{name:>14} {s['n']:>4} {s['wr']:>5.0f} {s['avg']:>+7.3f} "
                  f"{s['total']:>+8.1f} {s['sharpe']:>+6.2f}")


def main():
    print("=" * 72)
    print("WEEKEND: EXTENDED WINDOW + CORRELATION GATE BACKTEST")
    print("=" * 72)
    btc = load_btc_4h()
    btc = extend_btc_live(btc)
    macro = load_macro_data()
    ds = build_weekend_dataset(btc, macro)
    ds = augment_dataset_with_missing_predictors(ds)
    corr = build_corr_series(btc, macro)

    print(f"\nBTC 4h: {btc.index[0]} -- {btc.index[-1]}")

    # Head-to-head: validated 'net' rule (4-of-5) vs LIVE 'count' rule (3-of-5)
    print(f"\n{'='*72}\nTHRESHOLD RULE: validated (|net|>=3, 4of5) vs LIVE (count>=3, 3of5)\n{'='*72}")
    print(f"  (base exit Sun12 prod, 5% SL)")
    print(f"\n{'Rule':>20} {'Period':>12} {'N':>4} {'WR%':>6} {'Avg%':>8} {'Total%':>9} {'Sharpe':>7}")
    print("-" * 72)
    for rule, name in [("net", "net 4of5 (valid)"), ("count", "count 3of5 (LIVE)")]:
        tr = build_trades(btc, ds, corr, rule)
        for lo, hi, pname in [(2021, 2027, "full 5.4y"), (2026, 2027, "2026 only")]:
            sub = [t for t in tr if lo <= t["date"].year < hi]
            if len(sub) < 3:
                print(f"{name:>20} {pname:>12} {len(sub):>4}  (too few)")
                continue
            freq = len(sub) / max(yrs(sub), 0.1)
            s = stats([t["pnl_sl"]["Sun12 (prod)"] for t in sub], freq)
            if s:
                print(f"{name:>20} {pname:>12} {s['n']:>4} {s['wr']:>5.0f} "
                      f"{s['avg']:>+7.3f} {s['total']:>+8.1f} {s['sharpe']:>+6.2f}")

    trades = build_trades(btc, ds, corr, "net")
    longs = sum(t["direction"] == "long" for t in trades)
    print(f"Trades with signal: {len(trades)} (long={longs}, short={len(trades)-longs})")
    print(f"Period: {trades[0]['date'].date()} -- {trades[-1]['date'].date()} "
          f"({yrs(trades):.1f}y)")

    # Per-trade diagnostic for the live period (compare vs paper_trades.db)
    print(f"\n{'='*72}\nLIVE-PERIOD TRADES (backtest model) vs actual\n{'='*72}")
    print(f"{'date':>12} {'dir':>6} {'base%':>8} {'base+SL%':>9} {'corr':>6}")
    for t in trades:
        if t["date"] >= pd.Timestamp("2026-05-01", tz="UTC"):
            b = t["pnl"]["Sun12 (prod)"]
            bsl = t["pnl_sl"]["Sun12 (prod)"]
            print(f"{str(t['date'].date()):>12} {t['direction']:>6} "
                  f"{b:>+7.2f} {bsl:>+8.2f} {t['corr']:>+5.2f}")

    report_window(trades, "pnl", "H1: EXTENDED HOLD WINDOW — NO STOP-LOSS")
    report_window(trades, "pnl_sl", "H1: EXTENDED HOLD WINDOW — WITH -2% STOP-LOSS")
    report_corr_gate(trades)
    report_subperiods(trades)
    print(f"\n{'='*72}\nDONE\n{'='*72}")


if __name__ == "__main__":
    main()
