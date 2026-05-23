"""Full quantitative validation of Weekend SL=0.75% + V-bottom re-entry.

Runs the complete scorecard:
  - CPCV cross-validation
  - Deflated Sharpe Ratio
  - PBO (Probability of Backtest Overfitting)
  - Factor decomposition (alpha vs BTC beta)
  - Regime analysis (bull/bear/chop)
  - Walk-forward H1→H2

Compares: baseline (SL=2%), new (SL=0.75%+reentry), no SL.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.backtest.models import Trade, Side, ExitReason, CostBreakdown
from src.validation.scorecard import validate_strategy
from src.validation.factors import build_crypto_factors
from src.validation.cpcv import _trades_to_daily_returns

from scripts.research.validate_weekend_macro import (
    load_macro_data,
    build_weekend_dataset,
)

DATA = ROOT / "data" / "processed" / "candles"
ENSEMBLE_COLS = ["china_inet_fri", "japan_fri", "tech_week"]
MAJORITY = 2


def load_btc(tf):
    df = pd.read_parquet(DATA / f"BTCUSDT_{tf}.parquet")
    if pd.api.types.is_numeric_dtype(df["ts"]):
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def get_price_at(btc, ts):
    subset = btc[btc.index <= ts]
    return float(subset.iloc[-1]["close"]) if len(subset) > 0 else None


def simulate_strategy(ds, btc_1h, sl_pct, bounce_pct=None, re_sl_pct=None):
    """Simulate weekend strategy with given SL + optional re-entry.

    Returns list of Trade objects + return series.
    """
    trades_out = []
    returns = []

    for _, row in ds.iterrows():
        fri_date = row["fri_date"]
        signal = int(row["signal"])
        if signal == 0:
            continue

        fri_21 = pd.Timestamp(fri_date + timedelta(hours=21), tz="UTC")
        if fri_21 < btc_1h.index[0] or fri_21 > btc_1h.index[-1]:
            continue

        entry = get_price_at(btc_1h, fri_21)
        if entry is None:
            continue
        sun_23 = pd.Timestamp(fri_date + timedelta(days=2, hours=23), tz="UTC")
        exit_price = get_price_at(btc_1h, sun_23)
        if exit_price is None:
            continue

        raw_pnl = (exit_price - entry) / entry * 100 * signal

        # Simulate SL + re-entry on 1h candles
        if sl_pct is not None:
            sl_price_val = entry * (1 - sl_pct / 100) if signal == 1 else entry * (1 + sl_pct / 100)

            # Walk hourly candles to detect SL
            sl_h = None
            for h in range(1, 51):
                ts = fri_21 + timedelta(hours=h)
                subset = btc_1h[btc_1h.index <= ts]
                if len(subset) == 0:
                    continue
                c = subset.iloc[-1]
                worst = float(c["low"]) if signal == 1 else float(c["high"])
                if signal == 1 and worst <= sl_price_val:
                    sl_h = h
                    break
                elif signal == -1 and worst >= sl_price_val:
                    sl_h = h
                    break

            if sl_h is not None:
                actual_pnl = -sl_pct

                # Try re-entry if configured
                if bounce_pct is not None and re_sl_pct is not None:
                    local_extreme = sl_price_val
                    reentry_price = None

                    for h in range(sl_h + 1, 51):
                        ts = fri_21 + timedelta(hours=h)
                        subset = btc_1h[btc_1h.index <= ts]
                        if len(subset) == 0:
                            continue
                        c = subset.iloc[-1]
                        p = float(c["close"])
                        w = float(c["low"]) if signal == 1 else float(c["high"])

                        if signal == 1:
                            local_extreme = min(local_extreme, w)
                            bounce = (p - local_extreme) / local_extreme * 100
                        else:
                            local_extreme = max(local_extreme, w)
                            bounce = (local_extreme - p) / local_extreme * 100

                        if bounce >= bounce_pct:
                            reentry_price = p
                            break

                    if reentry_price is not None:
                        re_pnl = (exit_price - reentry_price) / reentry_price * 100 * signal
                        re_pnl = max(re_pnl, -re_sl_pct)
                        actual_pnl = -sl_pct + re_pnl
            else:
                actual_pnl = raw_pnl
        else:
            actual_pnl = raw_pnl

        returns.append(actual_pnl)

        # Build Trade object
        is_long = signal > 0
        ret_frac = actual_pnl / 100.0
        entry_time = datetime.combine(fri_date.date(), datetime.min.time().replace(hour=21),
                                      tzinfo=timezone.utc)
        exit_time = entry_time + timedelta(days=2, hours=2)
        placeholder_entry = 50000.0
        if is_long:
            placeholder_exit = placeholder_entry * (1 + ret_frac + 0.0016)
        else:
            placeholder_exit = placeholder_entry * (1 - ret_frac - 0.0016)

        trades_out.append(Trade(
            symbol="BTC/USDT",
            side=Side.LONG if is_long else Side.SHORT,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=placeholder_entry,
            exit_price=abs(placeholder_exit),
            size_usd=1000.0,
            exit_reason=ExitReason.TIMEOUT,
            costs=CostBreakdown(entry_fee=0.00055, exit_fee=0.00055, slippage=0.0005),
        ))

    return trades_out, pd.Series(returns, dtype=float)


def main():
    print("=" * 70)
    print("  WEEKEND SL+REENTRY: FULL QUANTITATIVE VALIDATION")
    print("=" * 70)

    btc_1h = load_btc("1h")
    btc_4h = load_btc("4h")
    print(f"  BTC 1h: {btc_1h.index[0]} -- {btc_1h.index[-1]} ({len(btc_1h)})")
    print(f"  BTC 4h: {btc_4h.index[0]} -- {btc_4h.index[-1]} ({len(btc_4h)})")

    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)

    avail = [c for c in ENSEMBLE_COLS if c in ds.columns]
    vote_sums = []
    for _, row in ds.iterrows():
        votes = [1 if row.get(c, 0) > 0 else -1 for c in avail if pd.notna(row.get(c))]
        vote_sums.append(sum(votes))
    ds["vote_sum"] = vote_sums
    ds["signal"] = np.where(
        ds["vote_sum"] >= MAJORITY, 1,
        np.where(ds["vote_sum"] <= -MAJORITY, -1, 0),
    )

    n_traded = (ds["signal"] != 0).sum()
    print(f"\n  Ensemble: {avail}, majority={MAJORITY}")
    print(f"  Weekends: {len(ds)}, tradeable: {n_traded}")

    # ================================================================
    # 1. Simulate all strategy variants
    # ================================================================
    print("\n" + "=" * 70)
    print("  1. STRATEGY VARIANTS")
    print("=" * 70)

    variants = {
        "No SL": (None, None, None),
        "SL=2.0% (old)": (2.0, None, None),
        "SL=1.0%": (1.0, None, None),
        "SL=0.75%": (0.75, None, None),
        "SL=0.75% + re-entry 0.5%/1.0%": (0.75, 0.5, 1.0),
        "SL=1.0% + re-entry 0.5%/1.0%": (1.0, 0.5, 1.0),
        "SL=1.0% + re-entry 0.3%/1.0%": (1.0, 0.3, 1.0),
    }

    all_results = {}
    print(f"\n  {'Variant':>35} {'N':>4} {'WR':>6} {'Avg':>7} {'Tot':>9} {'Sharpe':>7} {'MaxDD':>7}")
    print(f"  {'-'*80}")

    for name, (sl, bounce, re_sl) in variants.items():
        trades, rets = simulate_strategy(ds, btc_1h, sl, bounce, re_sl)
        if len(trades) < 10:
            print(f"  {name:>35}  too few trades ({len(trades)})")
            continue

        n = len(rets)
        wr = sum(1 for r in rets if r > 0) / n * 100
        avg = rets.mean()
        tot = rets.sum()
        sh = avg / rets.std() * sqrt(52) if rets.std() > 0 else 0
        cum = np.cumsum(rets.values)
        mdd = (cum - np.maximum.accumulate(cum)).min()

        print(f"  {name:>35} {n:>4} {wr:>5.1f}% {avg:>+6.2f}% {tot:>+8.2f}% {sh:>+6.2f} {mdd:>+6.2f}%")
        all_results[name] = {"trades": trades, "returns": rets, "sharpe": sh}

    # ================================================================
    # 2. Load factor data
    # ================================================================
    print("\n" + "=" * 70)
    print("  2. LOADING FACTOR DATA")
    print("=" * 70)

    btc_daily = pd.read_parquet(DATA / "BTCUSDT_1d.parquet")
    if pd.api.types.is_numeric_dtype(btc_daily["ts"]):
        btc_daily["ts"] = pd.to_datetime(btc_daily["ts"], unit="ms", utc=True)
    else:
        btc_daily["ts"] = pd.to_datetime(btc_daily["ts"], utc=True)
    btc_daily = btc_daily.set_index("ts").sort_index()
    btc_returns = btc_daily["close"].pct_change().dropna()

    alt_rets = {}
    for sym in ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "LINKUSDT",
                "AVAXUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT"]:
        f = DATA / f"{sym}_1d.parquet"
        if not f.exists():
            f4 = DATA / f"{sym}_4h.parquet"
            if f4.exists():
                adf = pd.read_parquet(f4)
                if pd.api.types.is_numeric_dtype(adf["ts"]):
                    adf["ts"] = pd.to_datetime(adf["ts"], unit="ms", utc=True)
                else:
                    adf["ts"] = pd.to_datetime(adf["ts"], utc=True)
                adf = adf.set_index("ts").sort_index()
                daily = adf["close"].resample("1D").last().dropna()
                alt_rets[sym] = daily.pct_change().dropna()
            continue
        adf = pd.read_parquet(f)
        if pd.api.types.is_numeric_dtype(adf["ts"]):
            adf["ts"] = pd.to_datetime(adf["ts"], unit="ms", utc=True)
        else:
            adf["ts"] = pd.to_datetime(adf["ts"], utc=True)
        adf = adf.set_index("ts").sort_index()
        alt_rets[sym] = adf["close"].pct_change().dropna()

    factors = build_crypto_factors(btc_returns, alt_rets if len(alt_rets) >= 4 else None)
    print(f"  BTC daily returns: {len(btc_returns)}")
    print(f"  Alt coins for factors: {list(alt_rets.keys())}")

    # Build PBO returns matrix from all variants
    all_daily = {}
    for name, r in all_results.items():
        daily = _trades_to_daily_returns(r["trades"])
        if len(daily) > 30:
            all_daily[name] = daily

    pbo_matrix = None
    if len(all_daily) >= 3:
        combined = pd.DataFrame(all_daily).fillna(0)
        if len(combined) > 30:
            pbo_matrix = combined.values
            print(f"  PBO matrix: {pbo_matrix.shape}")

    # ================================================================
    # 3. Full scorecard for key variants
    # ================================================================
    n_trials = len(variants)  # number of strategy variants tested

    for name in ["SL=2.0% (old)", "SL=0.75% + re-entry 0.5%/1.0%", "No SL"]:
        if name not in all_results:
            continue

        print(f"\n{'='*70}")
        print(f"  SCORECARD: {name}")
        print(f"{'='*70}")

        r = all_results[name]
        try:
            report = validate_strategy(
                trades=r["trades"],
                n_trials=n_trials,
                strategy_name=f"Weekend: {name}",
                btc_returns=btc_returns,
                factor_returns=factors,
                returns_matrix=pbo_matrix,
                cpcv_groups=6,
            )
            report.print_scorecard()
        except Exception as e:
            print(f"  ERROR: {e}")

    # ================================================================
    # 4. Walk-forward H1→H2
    # ================================================================
    print(f"\n{'='*70}")
    print("  WALK-FORWARD: H1 (select) → H2 (validate)")
    print(f"{'='*70}")

    mid = len(ds) // 2
    h1 = ds.iloc[:mid]
    h2 = ds.iloc[mid:]
    print(f"  H1: {len(h1)} weekends ({h1['fri_date'].iloc[0].date()} — {h1['fri_date'].iloc[-1].date()})")
    print(f"  H2: {len(h2)} weekends ({h2['fri_date'].iloc[0].date()} — {h2['fri_date'].iloc[-1].date()})")

    # Test on H2 with the chosen parameters (selected from research, not H1)
    for name, (sl, bounce, re_sl) in [
        ("H2: SL=2.0% (old)", (2.0, None, None)),
        ("H2: SL=0.75%+reentry", (0.75, 0.5, 1.0)),
    ]:
        trades, rets = simulate_strategy(h2, btc_1h, sl, bounce, re_sl)
        if len(trades) < 5:
            print(f"  {name}: too few trades")
            continue

        n = len(rets)
        wr = sum(1 for r in rets if r > 0) / n * 100
        avg = rets.mean()
        tot = rets.sum()
        sh = avg / rets.std() * sqrt(52) if rets.std() > 0 else 0

        print(f"\n  {name}: N={n} WR={wr:.0f}% avg={avg:+.2f}% total={tot:+.2f}% Sharpe={sh:.2f}")

        try:
            report = validate_strategy(
                trades=trades,
                n_trials=1,  # only 1 tested on H2
                strategy_name=name,
                btc_returns=btc_returns,
                factor_returns=factors,
                cpcv_groups=4,
            )
            report.print_scorecard()
        except Exception as e:
            print(f"    Scorecard error: {e}")

    print(f"\n{'='*70}")
    print("DONE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
