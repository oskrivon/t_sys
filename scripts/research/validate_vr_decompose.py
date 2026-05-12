"""Volume Ranking: decompose returns into factor vs alpha.

Key question: is VR alpha or just CSMB (size) factor exposure?
If CSMB beta=3.10 explains most returns, VR is just a leveraged
bet on small caps outperforming large caps.

Tests:
  1. Factor regression (full + rolling)
  2. Residual returns after hedging CSMB
  3. Sharpe of residuals = "true alpha"
  4. Is volume signal adding anything beyond size sorting?

Usage:
    python scripts/research/validate_vr_decompose.py
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.backtest_volume_ranking import load_daily_data, backtest_volume_ranking
from src.validation.factors import factor_decomposition, build_crypto_factors
from src.validation.regime import detect_regimes, regime_analysis
from src.validation.statistical import deflated_sharpe_from_returns

DATA = ROOT / "data" / "processed" / "candles"


def load_factors() -> pd.DataFrame:
    """Load BTC + alt returns and build factor matrix."""
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

    return build_crypto_factors(btc_ret, alts if len(alts) >= 4 else None)


def main():
    print("=" * 70)
    print("  VOLUME RANKING: FACTOR DECOMPOSITION DEEP DIVE")
    print("=" * 70)

    # 1. Run VR backtest
    print("\n1. Running Volume Ranking backtest...")
    datasets = load_daily_data()
    result = backtest_volume_ranking(datasets, short_window=7, long_window=30)
    print(f"   {len(result)} days, total return: {(np.prod(1 + result['net_ret'].values) - 1):.1%}")

    # Build daily return series with UTC index
    dates = pd.to_datetime(result["date"])
    if dates.dt.tz is None:
        dates = dates.dt.tz_localize("UTC")
    vr_returns = pd.Series(
        result["net_ret"].values,
        index=dates,
        name="vr_returns",
    )

    # 2. Load factors
    print("\n2. Loading factor data...")
    factors = load_factors()
    print(f"   Factors: {list(factors.columns)}, {len(factors)} days")

    # 3. Full-sample factor regression
    print("\n3. Full-sample factor regression:")
    full_result = factor_decomposition(vr_returns, factors)
    print(full_result.summary())

    # 4. Compute residual returns (alpha-only)
    print("\n4. Hedged returns (factor-neutral residuals):")
    common = vr_returns.index.intersection(factors.index)
    vr_aligned = vr_returns.loc[common].values
    X = factors.loc[common].values

    # Predicted factor component
    betas = np.array([full_result.betas[f] for f in full_result.factor_names])
    factor_component = X @ betas
    residuals = vr_aligned - factor_component

    # Residual Sharpe
    res_mean = residuals.mean()
    res_std = residuals.std()
    res_sharpe = res_mean / res_std * sqrt(365) if res_std > 0 else 0
    res_total = residuals.sum()

    print(f"   Residual (alpha-only) returns:")
    print(f"     Mean daily:   {res_mean*100:.4f}%")
    print(f"     Std daily:    {res_std*100:.4f}%")
    print(f"     Sharpe:       {res_sharpe:.2f}")
    print(f"     Total return: {res_total*100:.1f}%")

    # DSR on residuals
    dsr = deflated_sharpe_from_returns(residuals, n_trials=1)
    print(f"     DSR (n_trials=1): p={dsr.p_value:.3f} {'PASS' if dsr.is_significant else 'FAIL'}")

    # 5. Compare: which factor contributes most return?
    print("\n5. Return attribution:")
    total_vr = vr_aligned.sum()
    alpha_contribution = full_result.alpha * len(common)
    print(f"   Total VR return:     {total_vr*100:.1f}%")
    print(f"   Alpha contribution:  {alpha_contribution*100:.1f}% ({alpha_contribution/total_vr*100:.0f}%)")

    for fname in full_result.factor_names:
        beta = full_result.betas[fname]
        factor_ret = factors.loc[common, fname].sum()
        contribution = beta * factor_ret
        pct = contribution / total_vr * 100 if total_vr != 0 else 0
        print(f"   {fname:>8s} contribution: {contribution*100:.1f}% ({pct:.0f}% of total)")

    # 6. Rolling factor exposure (is it stable?)
    print("\n6. Rolling factor beta (60-day window):")
    window = 60
    rolling_betas = {"CMKT": [], "CMOM": [], "CSMB": [], "dates": []}

    for i in range(window, len(common)):
        window_vr = vr_aligned[i-window:i]
        window_X = X[i-window:i]
        window_factors_df = pd.DataFrame(
            window_X, columns=full_result.factor_names,
            index=common[i-window:i],
        )
        window_vr_series = pd.Series(window_vr, index=common[i-window:i])

        r = factor_decomposition(window_vr_series, window_factors_df)
        for fname in full_result.factor_names:
            if fname in rolling_betas:
                rolling_betas[fname].append(r.betas.get(fname, 0))
        rolling_betas["dates"].append(common[i])

    for fname in ["CMKT", "CMOM", "CSMB"]:
        vals = rolling_betas[fname]
        if vals:
            print(f"   {fname}: mean={np.mean(vals):.3f} std={np.std(vals):.3f} "
                  f"min={np.min(vals):.3f} max={np.max(vals):.3f}")

    # 7. Key question: volume signal vs pure size sort
    print("\n7. Volume signal vs pure size sort:")
    print("   Testing: what if we sort by volatility (size proxy) instead of volume ratio?")

    # Run a "pure size" strategy: sort by 20-day vol instead of volume ratio
    result_size = backtest_size_sort(datasets)
    if result_size is not None and len(result_size) > 30:
        size_rets = result_size["net_ret"].values
        size_sharpe = np.mean(size_rets) / np.std(size_rets) * sqrt(365)
        size_total = np.prod(1 + size_rets) - 1

        vr_sharpe = np.mean(result["net_ret"].values) / np.std(result["net_ret"].values) * sqrt(365)
        vr_total = np.prod(1 + result["net_ret"].values) - 1

        print(f"   Volume Ranking:  Sharpe={vr_sharpe:.2f}, Total={vr_total:.1%}")
        print(f"   Pure Size Sort:  Sharpe={size_sharpe:.2f}, Total={size_total:.1%}")
        print(f"   Difference:      Sharpe={vr_sharpe - size_sharpe:+.2f}")

        if vr_sharpe > size_sharpe + 0.3:
            print("   -> Volume signal adds value BEYOND pure size sorting")
        elif vr_sharpe > size_sharpe:
            print("   -> Volume signal adds marginal value over pure size")
        else:
            print("   -> Volume signal does NOT add value -- VR = pure size play")

        # Correlation between the two
        min_len = min(len(result), len(result_size))
        corr = np.corrcoef(
            result["net_ret"].values[:min_len],
            result_size["net_ret"].values[:min_len],
        )[0, 1]
        print(f"   Correlation: {corr:.2f}")

    # 8. Verdict
    print("\n" + "=" * 70)
    print("  VERDICT")
    print("=" * 70)
    if res_sharpe > 0.5 and dsr.is_significant:
        print("  Alpha EXISTS after factor hedging.")
        print(f"  Residual Sharpe: {res_sharpe:.2f}")
        print("  -> VR has genuine alpha beyond size exposure.")
        print("  -> Can trade as-is, but understand you're also taking size risk.")
    elif res_sharpe > 0:
        print("  Weak residual alpha detected but NOT statistically significant.")
        print(f"  Residual Sharpe: {res_sharpe:.2f}, DSR p={dsr.p_value:.3f}")
        print("  -> VR is MOSTLY size factor exposure with a thin volume-timing layer.")
        print("  -> Consider: is size exposure desirable? If yes, keep. If not, hedge.")
    else:
        print("  NO alpha after factor hedging.")
        print(f"  Residual Sharpe: {res_sharpe:.2f}")
        print("  -> VR is PURELY a size factor play. Volume signal adds nothing.")
        print("  -> Replace with explicit CSMB factor if you want size exposure.")


def backtest_size_sort(datasets: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """Pure size sort: long high-vol (small proxy), short low-vol (large proxy)."""

    all_dates = None
    for df in datasets.values():
        dates = set(df.index)
        if all_dates is None:
            all_dates = dates
        else:
            all_dates = all_dates.intersection(dates)
    all_dates = sorted(all_dates)

    if len(all_dates) < 40:
        return None

    daily_returns = []
    prev_longs = set()
    prev_shorts = set()

    for i in range(25, len(all_dates)):
        date = all_dates[i]
        prev_date = all_dates[i - 1]

        # Compute 20-day volatility as size proxy
        vols = {}
        for symbol, df in datasets.items():
            mask = df.index <= prev_date
            if mask.sum() < 25:
                continue
            sub = df[mask]
            vol = sub["close"].pct_change().iloc[-20:].std()
            if vol > 0:
                vols[symbol] = vol

        if len(vols) < 10:
            continue

        sorted_symbols = sorted(vols.keys(), key=lambda s: vols[s], reverse=True)
        n = len(sorted_symbols)
        n_long = n // 2

        longs = set(sorted_symbols[:n_long])    # high vol = "small" proxy
        shorts = set(sorted_symbols[n_long:])    # low vol = "large" proxy

        long_rets = []
        for sym in longs:
            if sym in datasets and date in datasets[sym].index and prev_date in datasets[sym].index:
                ret = datasets[sym].loc[date, "close"] / datasets[sym].loc[prev_date, "close"] - 1
                long_rets.append(ret)

        short_rets = []
        for sym in shorts:
            if sym in datasets and date in datasets[sym].index and prev_date in datasets[sym].index:
                ret = datasets[sym].loc[date, "close"] / datasets[sym].loc[prev_date, "close"] - 1
                short_rets.append(-ret)

        if not long_rets or not short_rets:
            continue

        long_ret = np.mean(long_rets)
        short_ret = np.mean(short_rets)
        gross_ret = (long_ret + short_ret) / 2

        new_longs = longs - prev_longs
        new_shorts = shorts - prev_shorts
        turnover = (len(new_longs) + len(new_shorts)) / max(1, n)
        fee_cost = turnover * 0.0004 * 2

        daily_returns.append({
            "date": date,
            "net_ret": gross_ret - fee_cost,
            "gross_ret": gross_ret,
        })

        prev_longs = longs
        prev_shorts = shorts

    return pd.DataFrame(daily_returns)


if __name__ == "__main__":
    main()
