"""Validate Size-Neutral Volume Ranking through the scorecard.

Also runs concentrated 20% variant for comparison.

Usage:
    python scripts/research/validate_vr_sizeneutral.py
"""
from __future__ import annotations

import sys
from datetime import timedelta, timezone
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.backtest.models import Trade, Side, ExitReason, CostBreakdown
from src.validation.factors import build_crypto_factors
from src.validation.scorecard import validate_strategy
from scripts.research.backtest_volume_ranking import load_daily_data

DATA = ROOT / "data" / "processed" / "candles"
FEE = 0.0004


def load_factors():
    btc = pd.read_parquet(DATA / "BTCUSDT_1d.parquet")
    btc.index = pd.to_datetime(btc["ts"], utc=True)
    btc_ret = btc["close"].pct_change().dropna()
    alts = {}
    for sym in ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "LINKUSDT",
                "AVAXUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT"]:
        f = DATA / f"{sym}_1d.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        df.index = pd.to_datetime(df["ts"], utc=True)
        alts[sym] = df["close"].pct_change().dropna()
    return btc_ret, build_crypto_factors(btc_ret, alts if len(alts) >= 4 else None)


def backtest_size_neutral_vr(
    datasets: dict[str, pd.DataFrame],
    short_window: int = 7,
    long_window: int = 30,
) -> pd.DataFrame:
    """Size-neutral VR: rank by volume ratio WITHIN size buckets."""
    daily_ret = {sym: df["close"].pct_change() for sym, df in datasets.items()}

    all_dates = None
    for df in datasets.values():
        dates = set(df.index)
        all_dates = dates if all_dates is None else all_dates.intersection(dates)
    all_dates = sorted(all_dates)

    results = []
    prev_longs, prev_shorts = set(), set()

    for i in range(long_window + 5, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        signals = {}
        for sym, df in datasets.items():
            if date not in df.index:
                continue
            mask = df.index <= prev
            if mask.sum() < long_window + 5:
                continue
            sub = df[mask]
            v_short = sub["volume"].iloc[-short_window:].mean()
            v_long = sub["volume"].iloc[-long_window:].mean()
            vol_20 = sub["close"].pct_change().iloc[-20:].std()

            if v_long > 0 and vol_20 > 0:
                signals[sym] = {"vol_ratio": v_short / v_long, "size_vol": vol_20}

        if len(signals) < 10:
            continue

        # Split into 2 size buckets
        sorted_by_size = sorted(signals.keys(), key=lambda s: signals[s]["size_vol"])
        mid = len(sorted_by_size) // 2
        large_bucket = sorted_by_size[:mid]
        small_bucket = sorted_by_size[mid:]

        # Within each bucket: long top half by vol_ratio, short bottom half
        longs, shorts = set(), set()
        for bucket in [large_bucket, small_bucket]:
            ranked = sorted(bucket, key=lambda s: signals[s]["vol_ratio"], reverse=True)
            half = len(ranked) // 2
            longs.update(ranked[:half])
            shorts.update(ranked[half:])

        l_rets = [daily_ret[s][date] for s in longs
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]
        s_rets = [-daily_ret[s][date] for s in shorts
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]

        if l_rets and s_rets:
            gross = (np.mean(l_rets) + np.mean(s_rets)) / 2
            new_l = longs - prev_longs
            new_s = shorts - prev_shorts
            turnover = (len(new_l) + len(new_s)) / max(1, len(signals))
            fee_cost = turnover * FEE * 2
            results.append({
                "date": date, "net_ret": gross - fee_cost, "gross_ret": gross,
                "fee_cost": fee_cost, "turnover": turnover,
            })

        prev_longs, prev_shorts = longs, shorts

    return pd.DataFrame(results)


def backtest_concentrated_vr(
    datasets: dict[str, pd.DataFrame],
    short_window: int = 7,
    long_window: int = 30,
    quantile: float = 0.2,
) -> pd.DataFrame:
    """Concentrated VR: long top quantile, short bottom quantile."""
    daily_ret = {sym: df["close"].pct_change() for sym, df in datasets.items()}

    all_dates = None
    for df in datasets.values():
        dates = set(df.index)
        all_dates = dates if all_dates is None else all_dates.intersection(dates)
    all_dates = sorted(all_dates)

    results = []
    prev_longs, prev_shorts = set(), set()

    for i in range(long_window + 5, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        ratios = {}
        for sym, df in datasets.items():
            if date not in df.index:
                continue
            mask = df.index <= prev
            if mask.sum() < long_window + 5:
                continue
            sub = df[mask]
            v_short = sub["volume"].iloc[-short_window:].mean()
            v_long = sub["volume"].iloc[-long_window:].mean()
            if v_long > 0:
                ratios[sym] = v_short / v_long

        if len(ratios) < 10:
            continue

        sorted_syms = sorted(ratios.keys(), key=lambda s: ratios[s], reverse=True)
        n = len(sorted_syms)
        q = max(2, int(n * quantile))
        longs = set(sorted_syms[:q])
        shorts = set(sorted_syms[-q:])

        l_rets = [daily_ret[s][date] for s in longs
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]
        s_rets = [-daily_ret[s][date] for s in shorts
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]

        if l_rets and s_rets:
            gross = (np.mean(l_rets) + np.mean(s_rets)) / 2
            new_l = longs - prev_longs
            new_s = shorts - prev_shorts
            turnover = (len(new_l) + len(new_s)) / max(1, n)
            fee_cost = turnover * FEE * 2
            results.append({
                "date": date, "net_ret": gross - fee_cost, "gross_ret": gross,
                "fee_cost": fee_cost, "turnover": turnover,
            })

        prev_longs, prev_shorts = longs, shorts

    return pd.DataFrame(results)


def result_to_trades(result: pd.DataFrame) -> list[Trade]:
    """Convert daily portfolio returns to Trade objects."""
    trades = []
    for _, row in result.iterrows():
        date = pd.Timestamp(row["date"])
        entry_time = date.to_pydatetime()
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        exit_time = entry_time + timedelta(days=1)

        entry_price = 100.0
        exit_price = 100.0 * (1 + row["net_ret"] + row["fee_cost"])

        trades.append(Trade(
            symbol="PORTFOLIO",
            side=Side.LONG,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            size_usd=10000.0,
            exit_reason=ExitReason.REBALANCE,
            costs=CostBreakdown(entry_fee=row["fee_cost"] / 2, exit_fee=row["fee_cost"] / 2),
        ))
    return trades


def build_pbo_matrix(datasets, backtest_fn, param_grid) -> np.ndarray | None:
    """Run multiple parameter variants for PBO."""
    variants = []
    for params in param_grid:
        r = backtest_fn(datasets, **params)
        if len(r) > 30:
            variants.append(r["net_ret"].values)

    if len(variants) < 3:
        return None

    min_len = min(len(v) for v in variants)
    return np.column_stack([v[:min_len] for v in variants])


def main():
    print("=" * 70)
    print("  SIZE-NEUTRAL VR + CONCENTRATED VR: VALIDATION")
    print("=" * 70)

    print("\nLoading data...")
    datasets = load_daily_data()
    btc_returns, factors = load_factors()
    print(f"  {len(datasets)} symbols, factors: {list(factors.columns)}")

    # ================================================================
    # 1. Size-Neutral VR
    # ================================================================
    print("\n" + "=" * 70)
    print("  STRATEGY: SIZE-NEUTRAL VOLUME RANKING")
    print("=" * 70)

    sn_result = backtest_size_neutral_vr(datasets)
    sn_rets = sn_result["net_ret"].values
    sn_total = np.prod(1 + sn_rets) - 1
    sn_sharpe = np.mean(sn_rets) / np.std(sn_rets) * sqrt(365)
    print(f"  {len(sn_result)} days, total={sn_total:.1%}, Sharpe={sn_sharpe:.2f}")

    sn_trades = result_to_trades(sn_result)

    # PBO: test parameter variants
    print("  Building PBO matrix...")
    sn_pbo_grid = [
        {"short_window": sw, "long_window": lw}
        for sw in [5, 7, 10, 14]
        for lw in [20, 30, 45, 60]
        if sw < lw
    ]
    sn_pbo_matrix = build_pbo_matrix(datasets, backtest_size_neutral_vr, sn_pbo_grid)
    if sn_pbo_matrix is not None:
        print(f"  PBO matrix: {sn_pbo_matrix.shape}")

    sn_report = validate_strategy(
        trades=sn_trades,
        n_trials=len(sn_pbo_grid),
        strategy_name="Size-Neutral VR (7d/30d)",
        btc_returns=btc_returns,
        factor_returns=factors,
        returns_matrix=sn_pbo_matrix,
        cpcv_groups=8,
    )
    sn_report.print_scorecard()

    # ================================================================
    # 2. Concentrated 20% VR
    # ================================================================
    print("\n" + "=" * 70)
    print("  STRATEGY: CONCENTRATED TOP/BOTTOM 20% VR")
    print("=" * 70)

    conc_result = backtest_concentrated_vr(datasets, quantile=0.2)
    conc_rets = conc_result["net_ret"].values
    conc_total = np.prod(1 + conc_rets) - 1
    conc_sharpe = np.mean(conc_rets) / np.std(conc_rets) * sqrt(365)
    print(f"  {len(conc_result)} days, total={conc_total:.1%}, Sharpe={conc_sharpe:.2f}")

    conc_trades = result_to_trades(conc_result)

    # PBO: vary window + quantile
    print("  Building PBO matrix...")
    conc_pbo_grid = [
        {"short_window": sw, "long_window": lw, "quantile": q}
        for sw in [5, 7, 10]
        for lw in [20, 30, 45]
        for q in [0.2, 0.3]
        if sw < lw
    ]
    conc_pbo_matrix = build_pbo_matrix(datasets, backtest_concentrated_vr, conc_pbo_grid)
    if conc_pbo_matrix is not None:
        print(f"  PBO matrix: {conc_pbo_matrix.shape}")

    conc_report = validate_strategy(
        trades=conc_trades,
        n_trials=len(conc_pbo_grid),
        strategy_name="Concentrated 20% VR (7d/30d)",
        btc_returns=btc_returns,
        factor_returns=factors,
        returns_matrix=conc_pbo_matrix,
        cpcv_groups=8,
    )
    conc_report.print_scorecard()

    # ================================================================
    # Summary comparison
    # ================================================================
    print("\n" + "=" * 70)
    print("  COMPARISON")
    print("=" * 70)
    print(f"  {'Metric':<25} {'Size-Neutral':>15} {'Concentrated 20%':>18} {'Original VR':>15}")
    print("  " + "-" * 75)

    # Get original VR for comparison
    from scripts.research.backtest_volume_ranking import backtest_volume_ranking
    orig = backtest_volume_ranking(datasets, 7, 30)
    orig_sharpe = np.mean(orig["net_ret"].values) / np.std(orig["net_ret"].values) * sqrt(365)
    orig_total = np.prod(1 + orig["net_ret"].values) - 1
    orig_dd = _max_dd(orig["net_ret"].values)

    sn_dd = _max_dd(sn_rets)
    conc_dd = _max_dd(conc_rets)

    print(f"  {'Sharpe':<25} {sn_sharpe:>15.2f} {conc_sharpe:>18.2f} {orig_sharpe:>15.2f}")
    print(f"  {'Total return':<25} {sn_total:>14.1%} {conc_total:>17.1%} {orig_total:>14.1%}")
    print(f"  {'Max drawdown':<25} {sn_dd:>14.1%} {conc_dd:>17.1%} {orig_dd:>14.1%}")

    cpcv_sn = sn_report.cpcv.median_sharpe if sn_report.cpcv else 0
    cpcv_conc = conc_report.cpcv.median_sharpe if conc_report.cpcv else 0
    print(f"  {'CPCV median Sharpe':<25} {cpcv_sn:>15.2f} {cpcv_conc:>18.2f} {'~1.7':>15}")

    dsr_sn = sn_report.dsr.p_value if sn_report.dsr else 1
    dsr_conc = conc_report.dsr.p_value if conc_report.dsr else 1
    print(f"  {'DSR p-value':<25} {dsr_sn:>15.3f} {dsr_conc:>18.3f} {'~0.58':>15}")

    pbo_sn = sn_report.pbo_result.pbo if sn_report.pbo_result else 0
    pbo_conc = conc_report.pbo_result.pbo if conc_report.pbo_result else 0
    print(f"  {'PBO':<25} {pbo_sn:>15.2f} {pbo_conc:>18.2f} {'~0.56':>15}")

    alpha_sn = sn_report.factor.alpha_tstat if sn_report.factor else 0
    alpha_conc = conc_report.factor.alpha_tstat if conc_report.factor else 0
    print(f"  {'Factor alpha t-stat':<25} {alpha_sn:>15.2f} {alpha_conc:>18.2f} {'~1.2':>15}")

    sn_pass = sn_report.n_passed
    conc_pass = conc_report.n_passed
    print(f"  {'Score':<25} {f'{sn_pass}/{sn_report.n_total}':>15} {f'{conc_pass}/{conc_report.n_total}':>18} {'2/7':>15}")


def _max_dd(rets):
    eq = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(eq)
    return float(((peak - eq) / peak).max())


if __name__ == "__main__":
    main()
