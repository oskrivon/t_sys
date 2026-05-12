"""Validate Weekend Effect with real macro predictors (yfinance).

Downloads 5 years of equity/macro data, reconstructs the full ensemble
signal pipeline, then runs the validation scorecard.

Predictors tested (from weekend_ensemble.py):
  - BABA friday return
  - NASDAQ (QQQ) friday return / week return
  - XLK (tech sector) week return
  - EWJ (Japan) friday return
  - KWEB (China internet) friday return
  - SPY friday return / week return
  - DXY proxy (UUP) friday return

Ensemble: majority vote of top-3 / top-5 predictors.

Usage:
    python scripts/research/validate_weekend_macro.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from itertools import combinations
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.backtest.models import Trade, Side, ExitReason, CostBreakdown
from src.validation.cpcv import cpcv_backtest, _trades_to_daily_returns
from src.validation.statistical import deflated_sharpe_from_returns, min_btl, pbo
from src.validation.factors import factor_decomposition, build_crypto_factors
from src.validation.regime import detect_regimes, regime_analysis
from src.validation.scorecard import validate_strategy

DATA = ROOT / "data" / "processed" / "candles"


# ======================================================================
# 1. Load data
# ======================================================================

def load_btc_4h() -> pd.DataFrame:
    """Load BTC 4h candles from parquet."""
    df = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index()


def load_macro_data() -> dict[str, pd.DataFrame]:
    """Download macro data via yfinance."""
    import yfinance as yf

    tickers = {
        "QQQ": "nq",
        "XLK": "tech",
        "EWJ": "japan",
        "BABA": "baba",
        "KWEB": "china_inet",
        "FXI": "china_lc",
        "SPY": "sp500",
        "DIA": "dow",
        "UUP": "dxy",
        "GLD": "gold",
    }

    print("  Downloading macro data via yfinance...")
    eq_dfs = {}
    for ticker, name in tickers.items():
        try:
            d = yf.download(ticker, start="2021-01-01", interval="1d", progress=False)
            if d.empty:
                print(f"    [WARN] No data for {ticker}")
                continue

            d = d.reset_index()
            # Handle MultiIndex columns from yfinance
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in d.columns]
            else:
                d.columns = [c.lower() for c in d.columns]

            if "date" in d.columns:
                d["date"] = pd.to_datetime(d["date"])
                if d["date"].dt.tz is not None:
                    d["date"] = d["date"].dt.tz_localize(None)
            elif "datetime" in d.columns:
                d["date"] = pd.to_datetime(d["datetime"])
                if d["date"].dt.tz is not None:
                    d["date"] = d["date"].dt.tz_localize(None)

            d["weekday"] = d["date"].dt.weekday
            eq_dfs[name] = d
            print(f"    {ticker} ({name}): {len(d)} days")
        except Exception as e:
            print(f"    [ERROR] {ticker}: {e}")

    return eq_dfs


def get_btc_price_at(btc: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """Get BTC close price at or before given timestamp."""
    subset = btc[btc.index <= ts]
    return float(subset.iloc[-1]["close"]) if len(subset) > 0 else None


# ======================================================================
# 2. Build weekend dataset with all predictor signals
# ======================================================================

def build_weekend_dataset(
    btc: pd.DataFrame,
    eq_dfs: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Build weekly dataset: Friday signals + BTC weekend returns."""

    # Use NASDAQ (nq) calendar as reference for Fridays
    ref_key = "nq" if "nq" in eq_dfs else list(eq_dfs.keys())[0]
    ref = eq_dfs[ref_key]

    rows = []
    for i in range(5, len(ref)):
        row = ref.iloc[i]
        if row["weekday"] != 4:  # Friday
            continue

        fri_date = row["date"]

        # BTC weekend return: Friday 21:00 UTC -> Sunday 23:00 UTC
        fri_21 = pd.Timestamp(fri_date + timedelta(hours=21), tz="UTC")
        sun_23 = pd.Timestamp(fri_date + timedelta(days=2, hours=23), tz="UTC")
        p_fri = get_btc_price_at(btc, fri_21)
        p_sun = get_btc_price_at(btc, sun_23)
        if not p_fri or not p_sun:
            continue

        btc_wk = (p_sun - p_fri) / p_fri * 100  # in percent

        signals: dict[str, float | datetime] = {
            "btc_weekend": btc_wk,
            "fri_date": fri_date,
        }

        # Compute signals for each equity predictor
        for name, edf in eq_dfs.items():
            fri_rows = edf[edf["date"] == fri_date]
            if len(fri_rows) == 0:
                continue
            fri_eq = fri_rows.iloc[0]

            # Friday return (intraday)
            if fri_eq["open"] > 0:
                fri_ret = (fri_eq["close"] - fri_eq["open"]) / fri_eq["open"] * 100
                signals[f"{name}_fri"] = fri_ret

            # Week return (Monday open -> Friday close)
            week = edf[(edf["date"] >= fri_date - timedelta(days=6)) & (edf["date"] <= fri_date)]
            mon = week[week["weekday"] == 0]
            if len(mon) > 0 and mon.iloc[0]["open"] > 0:
                week_ret = (fri_eq["close"] - mon.iloc[0]["open"]) / mon.iloc[0]["open"] * 100
                signals[f"{name}_week"] = week_ret

        rows.append(signals)

    df = pd.DataFrame(rows)
    print(f"  Built dataset: {len(df)} weekends, {len(df.columns)} columns")
    return df


# ======================================================================
# 3. Evaluate strategies + build Trade objects
# ======================================================================

def evaluate_ensemble(
    df: pd.DataFrame,
    combo: list[str],
    threshold: float | None = None,
    sl_pct: float = 2.0,
) -> tuple[list[Trade], pd.Series]:
    """Evaluate an ensemble voting strategy, return Trade objects + return series."""

    valid = df.dropna(subset=combo + ["btc_weekend", "fri_date"]).copy()
    combo_size = len(combo)
    if threshold is None:
        threshold = combo_size / 2

    # Majority vote
    valid["vote"] = sum(np.sign(valid[c]) for c in combo)
    valid["signal"] = np.where(
        valid["vote"] >= threshold, 1,
        np.where(valid["vote"] <= -threshold, -1, 0)
    )

    # Only trade when consensus
    trading = valid[valid["signal"] != 0].copy()
    if len(trading) < 10:
        return [], pd.Series(dtype=float)

    # Apply SL
    trading["raw_ret"] = trading["signal"] * trading["btc_weekend"]
    trading["ret"] = trading["raw_ret"].clip(lower=-sl_pct)

    # Build Trade objects
    trades = []
    for _, row in trading.iterrows():
        fri_date = row["fri_date"]
        is_long = row["signal"] > 0
        ret_pct = row["ret"] / 100.0  # convert from % to fraction

        entry_time = datetime.combine(fri_date.date(), datetime.min.time().replace(hour=21),
                                       tzinfo=timezone.utc)
        exit_time = entry_time + timedelta(days=2, hours=2)  # Sun 23:00

        entry_price = 50000.0  # placeholder, actual not needed for PnL
        if is_long:
            exit_price = entry_price * (1 + ret_pct + 0.0016)  # add back fees+slippage
        else:
            exit_price = entry_price * (1 - ret_pct - 0.0016)

        trades.append(Trade(
            symbol="BTC/USDT",
            side=Side.LONG if is_long else Side.SHORT,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=abs(exit_price),
            size_usd=1000.0,
            exit_reason=ExitReason.TIMEOUT,
            costs=CostBreakdown(entry_fee=0.00055, exit_fee=0.00055, slippage=0.0005),
        ))

    return trades, trading["ret"]


def run_all_ensembles(df: pd.DataFrame) -> dict[str, dict]:
    """Run all individual + ensemble strategies, collect results."""

    # Top predictors identified in original research
    predictor_cols = [c for c in df.columns if c.endswith("_fri") or c.endswith("_week")]
    predictor_cols = [c for c in predictor_cols if c not in ("fri_date",)]

    results = {}

    # Individual predictors
    for col in predictor_cols:
        trades, rets = evaluate_ensemble(df, [col], threshold=0.5)
        if len(trades) >= 20:
            wins = sum(1 for t in trades if t.net_pnl_pct > 0)
            avg = np.mean(rets)
            sharpe = avg / rets.std() * sqrt(52) if rets.std() > 0 else 0
            results[col] = {
                "trades": trades, "returns": rets,
                "n": len(trades), "wr": wins / len(trades),
                "avg": avg, "sharpe": sharpe,
            }

    # Ensemble combos (size 3 and 5)
    top_candidates = ["baba_fri", "nq_fri", "tech_week", "nq_week",
                      "japan_fri", "china_inet_fri", "sp500_fri", "sp500_week"]
    available = [c for c in top_candidates if c in df.columns]

    for combo_size in [3, 4, 5]:
        for combo in combinations(available, combo_size):
            combo_name = "VOTE(" + "+".join(
                c.replace("_fri", "F").replace("_week", "W") for c in combo
            ) + ")"

            trades, rets = evaluate_ensemble(df, list(combo))
            if len(trades) < 15:
                continue

            wins = sum(1 for t in trades if t.net_pnl_pct > 0)
            avg = float(np.mean(rets))
            sharpe = avg / rets.std() * sqrt(52) if rets.std() > 0 else 0

            results[combo_name] = {
                "trades": trades, "returns": rets,
                "n": len(trades), "wr": wins / len(trades),
                "avg": avg, "sharpe": sharpe,
            }

    return results


# ======================================================================
# 4. Build PBO matrix from all strategy variants
# ======================================================================

def build_pbo_matrix(results: dict[str, dict]) -> np.ndarray | None:
    """Build returns matrix for PBO from all strategy variants."""

    # Collect daily returns for each variant
    all_daily = {}
    for name, r in results.items():
        daily = _trades_to_daily_returns(r["trades"])
        if len(daily) > 30:
            all_daily[name] = daily

    if len(all_daily) < 3:
        return None

    # Align to common date range
    combined = pd.DataFrame(all_daily)
    combined = combined.fillna(0)

    if len(combined) < 30:
        return None

    return combined.values


# ======================================================================
# 5. Main
# ======================================================================

def main():
    print("=" * 70)
    print("  WEEKEND EFFECT: FULL VALIDATION WITH MACRO PREDICTORS")
    print("=" * 70)

    # Load data
    print("\n1. Loading BTC data...")
    btc = load_btc_4h()
    print(f"   BTC 4h: {len(btc)} candles, {btc.index[0]} -- {btc.index[-1]}")

    print("\n2. Loading macro data...")
    eq_dfs = load_macro_data()

    print("\n3. Building weekend dataset...")
    df = build_weekend_dataset(btc, eq_dfs)

    if len(df) < 30:
        print("   [ERROR] Not enough weekends for analysis")
        return

    # Basic stats
    print(f"   BTC weekend return: mean={df['btc_weekend'].mean():.2f}%, "
          f"std={df['btc_weekend'].std():.2f}%")

    print("\n4. Evaluating all predictor strategies...")
    results = run_all_ensembles(df)

    # Sort by Sharpe
    sorted_results = sorted(results.items(), key=lambda x: x[1]["sharpe"], reverse=True)

    print(f"\n   Total strategies evaluated: {len(results)}")
    print(f"\n   {'Strategy':<45} {'N':>4} {'WR':>6} {'Avg%':>7} {'Sharpe':>7}")
    print("   " + "-" * 72)
    for name, r in sorted_results[:20]:
        print(f"   {name:<45} {r['n']:>4} {r['wr']:>5.0%} {r['avg']:>6.2f}% {r['sharpe']:>7.2f}")

    # ================================================================
    # 5. Build PBO matrix
    # ================================================================
    print("\n5. Building PBO matrix from all variants...")
    pbo_matrix = build_pbo_matrix(results)
    if pbo_matrix is not None:
        print(f"   Matrix: {pbo_matrix.shape} (days x strategies)")
    else:
        print("   [WARN] Not enough variants for PBO")

    # ================================================================
    # 6. Validate BEST ensemble
    # ================================================================
    print("\n6. Running validation scorecard on best ensemble...")

    if not sorted_results:
        print("   No valid strategies found")
        return

    best_name, best = sorted_results[0]
    print(f"   Best: {best_name} (Sharpe={best['sharpe']:.2f}, N={best['n']}, WR={best['wr']:.0%})")

    # Load factor data
    btc_daily = pd.read_parquet(DATA / "BTCUSDT_1d.parquet")
    btc_daily.index = pd.to_datetime(btc_daily["ts"], utc=True)
    btc_returns = btc_daily["close"].pct_change().dropna()

    alt_rets = {}
    for sym in ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "LINKUSDT",
                "AVAXUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT"]:
        f = DATA / f"{sym}_1d.parquet"
        if not f.exists():
            continue
        adf = pd.read_parquet(f)
        adf.index = pd.to_datetime(adf["ts"], utc=True)
        alt_rets[sym] = adf["close"].pct_change().dropna()

    factors = build_crypto_factors(btc_returns, alt_rets if len(alt_rets) >= 4 else None)

    # How many trials? All unique strategies tested
    n_trials = len(results)
    print(f"   N trials (for DSR/MinBTL): {n_trials}")

    report = validate_strategy(
        trades=best["trades"],
        n_trials=n_trials,
        strategy_name=f"Weekend: {best_name}",
        btc_returns=btc_returns,
        factor_returns=factors,
        returns_matrix=pbo_matrix,
        cpcv_groups=6,
    )
    report.print_scorecard()

    # ================================================================
    # 7. Also validate the top-3 for comparison
    # ================================================================
    print("\n7. Top-3 strategies comparison:")
    print(f"   {'Strategy':<45} {'CPCV':>6} {'DSR':>6} {'PBO':>6} {'Alpha':>6}")
    print("   " + "-" * 72)

    for name, r in sorted_results[:3]:
        try:
            rep = validate_strategy(
                trades=r["trades"],
                n_trials=n_trials,
                strategy_name=name,
                btc_returns=btc_returns,
                factor_returns=factors,
                returns_matrix=pbo_matrix,
                cpcv_groups=6,
            )
            cpcv_val = rep.cpcv.median_sharpe if rep.cpcv else 0
            dsr_val = rep.dsr.p_value if rep.dsr else 1
            pbo_val = rep.pbo_result.pbo if rep.pbo_result else 0
            alpha_t = rep.factor.alpha_tstat if rep.factor else 0

            print(f"   {name:<45} {cpcv_val:>5.2f} {dsr_val:>5.3f} {pbo_val:>5.2f} {alpha_t:>5.2f}")
        except Exception as e:
            print(f"   {name:<45} ERROR: {str(e)[:40]}")

    # ================================================================
    # 8. Walk-forward: H1 select -> H2 validate
    # ================================================================
    print("\n8. Walk-forward validation (H1 select -> H2 test):")
    mid = len(df) // 2
    h1 = df.iloc[:mid]
    h2 = df.iloc[mid:]
    print(f"   H1: {len(h1)} weekends, H2: {len(h2)} weekends")

    # Find best on H1
    h1_results = {}
    predictor_cols = [c for c in df.columns if c.endswith("_fri") or c.endswith("_week")]
    top_candidates = ["baba_fri", "nq_fri", "tech_week", "nq_week",
                      "japan_fri", "china_inet_fri", "sp500_fri", "sp500_week"]
    available = [c for c in top_candidates if c in df.columns]

    for combo_size in [3, 5]:
        for combo in combinations(available, combo_size):
            combo_name = "VOTE(" + "+".join(
                c.replace("_fri", "F").replace("_week", "W") for c in combo
            ) + ")"
            trades, rets = evaluate_ensemble(h1, list(combo))
            if len(trades) >= 10:
                avg = float(np.mean(rets))
                sharpe = avg / rets.std() * sqrt(52) if rets.std() > 0 else 0
                h1_results[combo_name] = {"sharpe": sharpe, "combo": list(combo)}

    if h1_results:
        best_h1_name = max(h1_results, key=lambda k: h1_results[k]["sharpe"])
        best_h1 = h1_results[best_h1_name]
        print(f"   H1 best: {best_h1_name} (Sharpe={best_h1['sharpe']:.2f})")

        # Evaluate on H2
        h2_trades, h2_rets = evaluate_ensemble(h2, best_h1["combo"])
        if len(h2_trades) >= 5:
            wins = sum(1 for t in h2_trades if t.net_pnl_pct > 0)
            avg = float(np.mean(h2_rets))
            sharpe = avg / h2_rets.std() * sqrt(52) if h2_rets.std() > 0 else 0
            print(f"   H2 result: N={len(h2_trades)} WR={wins/len(h2_trades):.0%} "
                  f"Avg={avg:.2f}% Sharpe={sharpe:.2f}")

            # Validate H2 trades
            h2_report = validate_strategy(
                trades=h2_trades,
                n_trials=1,  # only 1 strategy tested on H2 (selected from H1)
                strategy_name=f"Weekend H2-OOS: {best_h1_name}",
                btc_returns=btc_returns,
                factor_returns=factors,
                cpcv_groups=4,
            )
            h2_report.print_scorecard()
        else:
            print(f"   H2: too few trades ({len(h2_trades)})")

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
