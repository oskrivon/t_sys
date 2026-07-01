"""Weekend MFE analysis + trailing breakeven backtest.

Answers: how often does the weekend trade hit unrealized profit
that then evaporates by Sunday settlement? And does a trailing
breakeven improve overall Sharpe?
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

DATA = ROOT / "data" / "processed" / "candles"

# Config — use available predictors from build_weekend_dataset + add XLE/USDJPY
PREDICTORS = ["china_inet_fri", "japan_fri", "tech_week", "energy_week", "usdjpy_week"]
MAJORITY = 3
SL_PCT = 0.75  # %


def augment_dataset_with_missing_predictors(ds: pd.DataFrame) -> pd.DataFrame:
    """Add energy_week (XLE) and usdjpy_week (USDJPY=X) if missing."""
    import yfinance as yf

    extra_tickers = {}
    if "energy_week" not in ds.columns:
        extra_tickers["XLE"] = "energy"
    if "usdjpy_week" not in ds.columns:
        extra_tickers["USDJPY=X"] = "usdjpy"

    if not extra_tickers:
        return ds

    for ticker, prefix in extra_tickers.items():
        print(f"  Downloading {ticker} for {prefix}_week...")
        data = yf.download(ticker, start="2020-01-01", interval="1d", progress=False)
        if data.empty:
            print(f"    FAILED: {ticker}")
            continue
        data = data.reset_index()
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in data.columns]
        else:
            data.columns = [c.lower() for c in data.columns]
        if "date" in data.columns:
            data["date"] = pd.to_datetime(data["date"])
            if data["date"].dt.tz is not None:
                data["date"] = data["date"].dt.tz_localize(None)
        data = data.set_index("date").sort_index()

        # Compute week return (Mon open -> Fri close) for each Friday in dataset
        week_rets = []
        for idx, row in ds.iterrows():
            fri_date = pd.Timestamp(row["fri_date"]) if "fri_date" in ds.columns else pd.Timestamp(idx)
            if fri_date.tzinfo:
                fri_date = fri_date.tz_localize(None)
            # Monday of same week
            mon_date = fri_date - pd.Timedelta(days=fri_date.weekday())

            mon_data = data[data.index >= mon_date]
            fri_data = data[data.index <= fri_date]

            if len(mon_data) == 0 or len(fri_data) == 0:
                week_rets.append(np.nan)
                continue

            mon_open = mon_data.iloc[0]["open"]
            fri_close = fri_data.iloc[-1]["close"]
            ret = (fri_close - mon_open) / mon_open * 100
            week_rets.append(ret)

        ds[f"{prefix}_week"] = week_rets
        print(f"    {prefix}_week: {sum(~pd.isna(ds[f'{prefix}_week']))} values")

    return ds


def load_btc_1h():
    df = pd.read_parquet(DATA / "BTCUSDT_1h.parquet")
    if pd.api.types.is_numeric_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def simulate_weekend_trades(btc_1h, ds):
    """Simulate all weekend trades with intra-weekend 1h resolution."""
    available = [c for c in PREDICTORS if c in ds.columns]
    print(f"Available predictors: {available}")

    trades = []
    for idx, row in ds.iterrows():
        votes = 0
        n_valid = 0
        for c in available:
            val = row.get(c, np.nan)
            if pd.isna(val):
                continue
            n_valid += 1
            votes += 1 if val > 0 else -1

        if n_valid < MAJORITY or abs(votes) < MAJORITY:
            continue
        direction = "long" if votes > 0 else "short"

        # Entry: Friday 21:00 UTC — date is in fri_date column
        fri_date = row.get("fri_date", idx)
        entry_ts = pd.Timestamp(fri_date).replace(hour=21, minute=0)
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.tz_localize("UTC")

        # Exit: Sunday 23:00
        exit_ts = entry_ts + pd.Timedelta(hours=50)

        # Get 1h candles during the weekend
        mask = (btc_1h.index >= entry_ts) & (btc_1h.index <= exit_ts)
        weekend_candles = btc_1h[mask]

        if len(weekend_candles) < 10:
            continue

        entry_price = weekend_candles.iloc[0]["close"]
        exit_price = weekend_candles.iloc[-1]["close"]

        # MFE/MAE
        if direction == "long":
            mfe_pct = (weekend_candles["high"].max() - entry_price) / entry_price * 100
            mae_pct = (entry_price - weekend_candles["low"].min()) / entry_price * 100
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            mfe_pct = (entry_price - weekend_candles["low"].min()) / entry_price * 100
            mae_pct = (weekend_candles["high"].max() - entry_price) / entry_price * 100
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        # Simulate trailing breakeven at various thresholds
        trail_results = {}
        for threshold in [0.2, 0.3, 0.4, 0.5, 0.75, 1.0]:
            triggered = False
            be_exit = False
            for _, candle in weekend_candles.iterrows():
                if direction == "long":
                    unrealized = (candle["high"] - entry_price) / entry_price * 100
                    if unrealized >= threshold:
                        triggered = True
                    if triggered and candle["low"] <= entry_price:
                        be_exit = True
                        break
                else:
                    unrealized = (entry_price - candle["low"]) / entry_price * 100
                    if unrealized >= threshold:
                        triggered = True
                    if triggered and candle["high"] >= entry_price:
                        be_exit = True
                        break

            trail_pnl = 0.0 if be_exit else pnl_pct
            trail_results[threshold] = {
                "triggered": triggered,
                "pnl": trail_pnl,
                "be_exit": be_exit,
            }

        # Simulate stepped trailing: move SL to +0.15% after +0.4%
        stepped_results = {}
        for trigger_at, lock_at in [(0.3, 0.0), (0.4, 0.15), (0.5, 0.2), (0.75, 0.3)]:
            triggered = False
            step_exit = False
            step_pnl = pnl_pct
            for _, candle in weekend_candles.iterrows():
                if direction == "long":
                    unrealized = (candle["high"] - entry_price) / entry_price * 100
                    if unrealized >= trigger_at:
                        triggered = True
                    if triggered:
                        lock_price = entry_price * (1 + lock_at / 100)
                        if candle["low"] <= lock_price:
                            step_exit = True
                            step_pnl = lock_at
                            break
                else:
                    unrealized = (entry_price - candle["low"]) / entry_price * 100
                    if unrealized >= trigger_at:
                        triggered = True
                    if triggered:
                        lock_price = entry_price * (1 - lock_at / 100)
                        if candle["high"] >= lock_price:
                            step_exit = True
                            step_pnl = lock_at
                            break
            stepped_results[(trigger_at, lock_at)] = {
                "triggered": triggered,
                "pnl": step_pnl,
                "exit": step_exit,
            }

        trade = {
            "date": entry_ts,
            "direction": direction,
            "entry": entry_price,
            "exit": exit_price,
            "pnl": pnl_pct,
            "mfe": mfe_pct,
            "mae": mae_pct,
        }
        for t, r in trail_results.items():
            trade[f"trail_{t}_pnl"] = r["pnl"]
            trade[f"trail_{t}_triggered"] = r["triggered"]
            trade[f"trail_{t}_be_exit"] = r["be_exit"]
        for (trig, lock), r in stepped_results.items():
            trade[f"step_{trig}_{lock}_pnl"] = r["pnl"]
            trade[f"step_{trig}_{lock}_exit"] = r["exit"]

        trades.append(trade)

    return pd.DataFrame(trades)


def print_stats(label, pnls, freq):
    n = len(pnls)
    if n < 5:
        print(f"  {label:<35} too few ({n})")
        return
    wr = (pnls > 0).mean() * 100
    avg = pnls.mean()
    tot = pnls.sum()
    sh = avg / pnls.std() * sqrt(freq) if pnls.std() > 0 else 0
    cum = np.cumsum(pnls)
    mdd = (cum - np.maximum.accumulate(cum)).min()
    print(f"  {label:<35} {n:>4} {wr:>5.1f}% {avg:>+7.3f}% {tot:>+8.1f}% {sh:>+6.2f} {mdd:>+7.2f}%")


def main():
    print("=" * 70)
    print("WEEKEND MFE + TRAILING BREAKEVEN ANALYSIS")
    print("=" * 70)

    btc_1h = load_btc_1h()
    btc_4h = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    btc_4h["ts"] = pd.to_datetime(btc_4h["ts"], utc=True)
    btc_4h = btc_4h.set_index("ts").sort_index()

    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)
    ds = augment_dataset_with_missing_predictors(ds)

    print(f"BTC 1h: {btc_1h.index[0]} -- {btc_1h.index[-1]} ({len(btc_1h)} candles)")
    print(f"Weekend dataset: {len(ds)} weekends\n")

    df = simulate_weekend_trades(btc_1h, ds)
    print(f"\nTotal trades with signal: {len(df)}")
    if len(df) == 0:
        print("NO TRADES — check predictor availability")
        return
    print(f"Direction: long={sum(df.direction=='long')}, short={sum(df.direction=='short')}")

    years = (df.date.max() - df.date.min()).days / 365.25
    freq = len(df) / years if years > 0 else 52

    # ── MFE Distribution ──
    print(f"\n{'='*70}")
    print("MFE DISTRIBUTION (max unrealized profit during weekend)")
    print(f"{'='*70}\n")
    for p in [10, 25, 50, 75, 90]:
        print(f"  P{p}: MFE={df.mfe.quantile(p/100):+.3f}%, MAE={df.mae.quantile(p/100):+.3f}%")
    print(f"  Mean: MFE={df.mfe.mean():+.3f}%, MAE={df.mae.mean():+.3f}%")

    # ── Missed Profit ──
    print(f"\n{'='*70}")
    print("MISSED PROFIT: MFE was positive but settled negative")
    print(f"{'='*70}\n")
    for mfe_th in [0.2, 0.3, 0.5, 0.75, 1.0]:
        had_profit = df[df.mfe >= mfe_th]
        settled_neg = had_profit[had_profit.pnl < 0]
        if len(had_profit) > 0:
            pct = len(settled_neg) / len(had_profit) * 100
            print(f"  MFE >= {mfe_th:.1f}%: {len(had_profit)} trades, "
                  f"{len(settled_neg)} settled negative ({pct:.0f}%)")

    # ── Trailing Breakeven Comparison ──
    print(f"\n{'='*70}")
    print("TRAILING BREAKEVEN vs BASELINE")
    print(f"{'='*70}\n")
    print(f"  {'Strategy':<35} {'N':>4} {'WR':>6} {'Avg':>8} {'Total':>9} {'Sharpe':>7} {'MaxDD':>8}")
    print(f"  {'-'*80}")

    # Baseline: no SL, hold to Sunday
    print_stats("Baseline (no SL)", df.pnl.values, freq)

    # With SL 0.75% (approximate: if MAE >= SL before MFE)
    # More accurate: simulate sequentially
    sl_pnls = np.where(df.mae.values >= SL_PCT, -SL_PCT, df.pnl.values)
    print_stats("+ SL 0.75%", sl_pnls, freq)

    print()
    for threshold in [0.2, 0.3, 0.4, 0.5, 0.75, 1.0]:
        col = f"trail_{threshold}_pnl"
        t_pnls = np.where(df.mae.values >= SL_PCT, -SL_PCT, df[col].values)
        be_count = int(df[f"trail_{threshold}_be_exit"].sum())
        trig_count = int(df[f"trail_{threshold}_triggered"].sum())
        label = f"SL 0.75% + BE@{threshold}%"
        print_stats(label, t_pnls, freq)
        print(f"    → triggered: {trig_count}, BE exits: {be_count}")

    # ── Stepped Trailing ──
    print(f"\n{'='*70}")
    print("STEPPED TRAILING (lock partial profit)")
    print(f"{'='*70}\n")
    print(f"  {'Strategy':<35} {'N':>4} {'WR':>6} {'Avg':>8} {'Total':>9} {'Sharpe':>7} {'MaxDD':>8}")
    print(f"  {'-'*80}")

    for (trig, lock) in [(0.3, 0.0), (0.4, 0.15), (0.5, 0.2), (0.75, 0.3)]:
        col = f"step_{trig}_{lock}_pnl"
        s_pnls = np.where(df.mae.values >= SL_PCT, -SL_PCT, df[col].values)
        exits = int(df[f"step_{trig}_{lock}_exit"].sum())
        label = f"Trig@{trig}% Lock@{lock}%"
        print_stats(label, s_pnls, freq)
        print(f"    → step exits: {exits}")

    # ── Case Studies ──
    print(f"\n{'='*70}")
    print("CASE STUDY: MFE >= 0.3% but settled negative")
    print(f"{'='*70}\n")
    missed = df[(df.mfe >= 0.3) & (df.pnl < 0)].sort_values("pnl")
    for _, t in missed.head(15).iterrows():
        be03 = t["trail_0.3_pnl"]
        print(f"  {t.date.strftime('%Y-%m-%d')} {t.direction:>5} "
              f"MFE={t.mfe:+.2f}% MAE={t.mae:+.2f}% "
              f"Settled={t.pnl:+.2f}% → BE@0.3%={be03:+.2f}%")

    # ── Annual breakdown ──
    print(f"\n{'='*70}")
    print("ANNUAL BREAKDOWN: Baseline vs SL 0.75% vs BE@0.3%")
    print(f"{'='*70}\n")
    df["year"] = df.date.dt.year
    print(f"  {'Year':<6} {'Base':>9} {'SL 0.75%':>9} {'BE@0.3%':>9} {'BE@0.5%':>9}")
    print(f"  {'-'*50}")
    for year in sorted(df.year.unique()):
        yr = df[df.year == year]
        base = yr.pnl.sum()
        sl = np.where(yr.mae.values >= SL_PCT, -SL_PCT, yr.pnl.values).sum()
        be03 = np.where(yr.mae.values >= SL_PCT, -SL_PCT, yr["trail_0.3_pnl"].values).sum()
        be05 = np.where(yr.mae.values >= SL_PCT, -SL_PCT, yr["trail_0.5_pnl"].values).sum()
        print(f"  {year:<6} {base:>+8.2f}% {sl:>+8.2f}% {be03:>+8.2f}% {be05:>+8.2f}%")

    print(f"\n{'='*70}")
    print("DONE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
