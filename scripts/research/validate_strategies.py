"""Run validation scorecard on all major strategies.

Strategies:
  1. Weekend Effect — reconstruct trades from BTC + macro data
  2. Miro + ML + Vision — load from ml_vision_combined_results.csv
  3. Volume Ranking L/S — run backtest, extract daily returns

Usage:
    python scripts/research/validate_strategies.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
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
REPORTS = ROOT / "data" / "reports"


# ======================================================================
# Shared: load BTC returns + factor data
# ======================================================================

def load_btc_daily() -> pd.Series:
    """Load BTC daily returns from parquet."""
    df = pd.read_parquet(DATA / "BTCUSDT_1d.parquet")
    df.index = pd.to_datetime(df["ts"], utc=True)
    returns = df["close"].pct_change().dropna()
    returns.name = "btc_returns"
    return returns


def load_alt_daily() -> dict[str, pd.Series]:
    """Load alt daily returns for factor construction."""
    alts = {}
    for sym in ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "LINKUSDT",
                "AVAXUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT"]:
        f = DATA / f"{sym}_1d.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        df.index = pd.to_datetime(df["ts"], utc=True)
        alts[sym] = df["close"].pct_change().dropna()
    return alts


def build_factors() -> pd.DataFrame:
    """Build crypto factor returns."""
    btc = load_btc_daily()
    alts = load_alt_daily()
    return build_crypto_factors(btc, alts if len(alts) >= 4 else None)


# ======================================================================
# 1. MIRO + ML + Vision
# ======================================================================

def load_miro_trades() -> list[Trade]:
    """Load Miro trades from ml_vision_combined_results.csv."""
    csv_path = REPORTS / "ml_vision_combined_results.csv"
    df = pd.read_csv(csv_path)
    print(f"  Loaded {len(df)} Miro+Vision trades")
    print(f"  Columns: {df.columns.tolist()}")
    print(f"  Vision scores: {df['vision_score'].value_counts().to_dict()}")

    # Load candle data to get actual prices and timestamps
    candle_cache: dict[str, pd.DataFrame] = {}

    trades = []
    for _, row in df.iterrows():
        symbol = row["symbol"].replace("/", "")
        is_long = bool(row["is_long"])
        pnl_pct = row["pnl_pct"]
        outcome = row["outcome"]

        # Load candles if needed
        if symbol not in candle_cache:
            path = DATA / f"{symbol}_4h.parquet"
            if path.exists():
                cdf = pd.read_parquet(path)
                cdf.index = pd.to_datetime(cdf["ts"], utc=True)
                candle_cache[symbol] = cdf
            else:
                continue

        cdf = candle_cache[symbol]
        entry_idx = int(row["entry_idx"]) if row["entry_idx"] < len(cdf) else len(cdf) - 2

        if entry_idx >= len(cdf) - 1:
            continue

        entry_row = cdf.iloc[entry_idx]
        entry_price = float(entry_row["close"])
        entry_time = entry_row.name.to_pydatetime()

        # Estimate exit: avg hold ~6 candles (24h) based on strategy params
        exit_idx = min(entry_idx + 6, len(cdf) - 1)
        exit_row = cdf.iloc[exit_idx]
        exit_time = exit_row.name.to_pydatetime()

        if is_long:
            exit_price = entry_price * (1 + pnl_pct + 0.0011)  # add back fees
        else:
            exit_price = entry_price * (1 - pnl_pct - 0.0011)

        side = Side.LONG if is_long else Side.SHORT
        exit_reason = ExitReason.TP if outcome == "win" else ExitReason.SL

        trades.append(Trade(
            symbol=row["symbol"],
            side=side,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=abs(exit_price),
            size_usd=1000.0,
            exit_reason=exit_reason,
            costs=CostBreakdown(entry_fee=0.00055, exit_fee=0.00055),
        ))

    print(f"  Converted {len(trades)} trades to Trade objects")
    return trades


# ======================================================================
# 2. VOLUME RANKING L/S
# ======================================================================

def run_volume_ranking() -> tuple[list[Trade], np.ndarray]:
    """Run volume ranking backtest, return trades + daily returns for variants."""
    from scripts.research.backtest_volume_ranking import load_daily_data, backtest_volume_ranking

    print("  Loading daily data...")
    datasets = load_daily_data()
    print(f"  Loaded {len(datasets)} symbols")

    # Run main variant
    result = backtest_volume_ranking(datasets, short_window=7, long_window=30)
    print(f"  Backtest: {len(result)} days")

    net_rets = result["net_ret"].values
    total_ret = np.prod(1 + net_rets) - 1
    sharpe = np.mean(net_rets) / np.std(net_rets) * sqrt(365)
    print(f"  Total return: {total_ret:.1%}, Sharpe: {sharpe:.2f}")

    # Convert portfolio-level daily returns to pseudo-trades
    # Volume Ranking is a daily-rebalance portfolio, not individual trades
    # Each day = one "trade" (portfolio return)
    trades = []
    for _, row in result.iterrows():
        date = pd.Timestamp(row["date"])
        entry_time = date.to_pydatetime()
        if not hasattr(entry_time, 'tzinfo') or entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        exit_time = entry_time + timedelta(days=1)

        net_ret = row["net_ret"]
        entry_price = 100.0
        exit_price = 100.0 * (1 + net_ret + row["fee_cost"])

        trades.append(Trade(
            symbol="PORTFOLIO",
            side=Side.LONG,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            size_usd=10000.0,
            exit_reason=ExitReason.REBALANCE,
            costs=CostBreakdown(
                entry_fee=row["fee_cost"] / 2,
                exit_fee=row["fee_cost"] / 2,
            ),
        ))

    # Run multiple parameter variants for PBO
    print("  Running parameter variants for PBO...")
    variants_returns = []
    for sw in [5, 7, 10, 14]:
        for lw in [20, 30, 45, 60]:
            if sw >= lw:
                continue
            r = backtest_volume_ranking(datasets, short_window=sw, long_window=lw)
            if len(r) > 0:
                # Align to same length
                variants_returns.append(r["net_ret"].values)

    # Align all variants to minimum length
    min_len = min(len(v) for v in variants_returns)
    returns_matrix = np.column_stack([v[:min_len] for v in variants_returns])
    print(f"  PBO matrix: {returns_matrix.shape} (days x variants)")

    return trades, returns_matrix


# ======================================================================
# 3. WEEKEND EFFECT (reconstruct from BTC data)
# ======================================================================

def reconstruct_weekend_trades() -> tuple[list[Trade], np.ndarray]:
    """Reconstruct weekend BTC trades from candle data.

    Uses the simplest weekend signal: BTC Friday close -> Sunday close.
    For validation purposes we test multiple 'signal' variants (random directions)
    to get a PBO matrix. The actual signal would come from macro predictors,
    but we don't have yfinance data cached. So we use BTC-only weekend returns
    with momentum signal (past week direction = weekend direction).
    """
    btc_4h = DATA / "BTCUSDT_4h.parquet"
    if not btc_4h.exists():
        print("  [WARN] No BTC 4h data, skipping weekend")
        return [], np.array([])

    df = pd.read_parquet(btc_4h)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()

    # Find Fridays (21:00 UTC) and Sundays (23:00 UTC)
    trades = []
    weekend_returns = []

    # Group by week
    df["date"] = df.index.date
    df["dow"] = df.index.dayofweek  # 0=Mon, 4=Fri, 6=Sun
    df["hour"] = df.index.hour

    # Get Friday 20:00 UTC candle (close = ~21:00) and Sunday 20:00 UTC candle
    fridays = df[(df["dow"] == 4) & (df["hour"] == 20)].copy()
    sundays = df[(df["dow"] == 6) & (df["hour"] == 20)].copy()

    # Get weekly returns for momentum signal
    df_daily = df.resample("1D").agg({"close": "last"}).dropna()
    df_daily["week_ret"] = df_daily["close"].pct_change(7)

    for fri_ts, fri_row in fridays.iterrows():
        # Find matching Sunday
        target_sun = fri_ts + timedelta(days=2)
        sun_matches = sundays[
            (sundays.index >= target_sun - timedelta(hours=4)) &
            (sundays.index <= target_sun + timedelta(hours=4))
        ]
        if sun_matches.empty:
            continue

        sun_row = sun_matches.iloc[0]
        sun_ts = sun_matches.index[0]

        entry_price = float(fri_row["close"])
        exit_price = float(sun_row["close"])
        weekend_ret = (exit_price - entry_price) / entry_price

        # Momentum signal: if past week was up, go long; if down, go short
        fri_date = pd.Timestamp(fri_ts.date(), tz="UTC")
        closest = df_daily.index[df_daily.index <= fri_date]
        if len(closest) == 0:
            continue
        week_ret = df_daily.loc[closest[-1], "week_ret"] if "week_ret" in df_daily.columns else 0

        if pd.isna(week_ret) or week_ret == 0:
            continue

        is_long = week_ret > 0
        if not is_long:
            weekend_ret = -weekend_ret

        # Apply 2% stop-loss
        weekend_ret_sl = max(weekend_ret, -0.02)
        weekend_returns.append(weekend_ret_sl)

        side = Side.LONG if is_long else Side.SHORT
        if is_long:
            adj_exit = entry_price * (1 + weekend_ret_sl)
        else:
            adj_exit = entry_price * (1 - weekend_ret_sl)

        trades.append(Trade(
            symbol="BTC/USDT",
            side=side,
            entry_time=fri_ts.to_pydatetime(),
            exit_time=sun_ts.to_pydatetime(),
            entry_price=entry_price,
            exit_price=abs(adj_exit),
            size_usd=1000.0,
            exit_reason=ExitReason.TIMEOUT,
            costs=CostBreakdown(entry_fee=0.00055, exit_fee=0.00055, slippage=0.0005),
        ))

    print(f"  Reconstructed {len(trades)} weekend trades")
    if trades:
        wins = sum(1 for t in trades if t.net_pnl_pct > 0)
        avg_pnl = np.mean([t.net_pnl_pct for t in trades])
        print(f"  WR: {wins/len(trades):.0%}, Avg PnL: {avg_pnl:.2%}")

    # Build PBO matrix: test multiple signal variants
    # Variant 1: momentum (what we used)
    # Variant 2-N: shift lookback (3d, 5d, 7d, 10d, 14d, 21d)
    print("  Building PBO matrix with signal variants...")
    btc_rets = np.array(weekend_returns)

    variants = [btc_rets]  # base variant
    for lookback in [3, 5, 10, 14, 21, 28]:
        df_daily[f"ret_{lookback}"] = df_daily["close"].pct_change(lookback)

    variant_returns_list = [weekend_returns]
    for lookback in [3, 5, 10, 14, 21, 28]:
        col = f"ret_{lookback}"
        v_rets = []
        for fri_ts, fri_row in fridays.iterrows():
            target_sun = fri_ts + timedelta(days=2)
            sun_matches = sundays[
                (sundays.index >= target_sun - timedelta(hours=4)) &
                (sundays.index <= target_sun + timedelta(hours=4))
            ]
            if sun_matches.empty:
                continue
            sun_row = sun_matches.iloc[0]
            entry_price = float(fri_row["close"])
            exit_price = float(sun_row["close"])
            raw_ret = (exit_price - entry_price) / entry_price

            fri_date = pd.Timestamp(fri_ts.date(), tz="UTC")
            closest = df_daily.index[df_daily.index <= fri_date]
            if len(closest) == 0:
                continue
            sig = df_daily.loc[closest[-1], col] if col in df_daily.columns else 0
            if pd.isna(sig) or sig == 0:
                continue

            directed_ret = raw_ret if sig > 0 else -raw_ret
            v_rets.append(max(directed_ret, -0.02))

        if len(v_rets) >= len(weekend_returns) * 0.8:
            variant_returns_list.append(v_rets)

    # Align lengths
    min_len = min(len(v) for v in variant_returns_list)
    returns_matrix = np.column_stack([np.array(v[:min_len]) for v in variant_returns_list])
    print(f"  PBO matrix: {returns_matrix.shape} (weekends x variants)")

    return trades, returns_matrix


# ======================================================================
# Main
# ======================================================================

def main():
    print("=" * 70)
    print("  STRATEGY VALIDATION PIPELINE")
    print("=" * 70)

    # Load shared data
    print("\nLoading shared data...")
    btc_returns = load_btc_daily()
    factors = build_factors()
    print(f"  BTC: {len(btc_returns)} days")
    print(f"  Factors: {list(factors.columns)}")

    # ================================================================
    # 1. MIRO + ML + Vision
    # ================================================================
    print("\n" + "=" * 70)
    print("  STRATEGY 1: MIRO + ML + VISION (score >= 8)")
    print("=" * 70)

    miro_trades = load_miro_trades()
    if miro_trades:
        # N_trials: we tested ~20 ML configs (regularization, features, thresholds)
        # + 4 vision score thresholds = ~80 total combinations
        miro_report = validate_strategy(
            trades=miro_trades,
            n_trials=80,
            strategy_name="Miro + ML + Vision>=8",
            btc_returns=btc_returns,
            factor_returns=factors,
            cpcv_groups=8,
        )
        miro_report.print_scorecard()

    # ================================================================
    # 2. VOLUME RANKING
    # ================================================================
    print("\n" + "=" * 70)
    print("  STRATEGY 2: VOLUME RANKING L/S")
    print("=" * 70)

    vr_trades, vr_matrix = run_volume_ranking()
    if vr_trades:
        # N_trials: 4 short windows x 4 long windows = ~12 configs
        vr_report = validate_strategy(
            trades=vr_trades,
            n_trials=12,
            strategy_name="Volume Ranking 7d/30d L/S",
            btc_returns=btc_returns,
            factor_returns=factors,
            returns_matrix=vr_matrix,
            cpcv_groups=8,
        )
        vr_report.print_scorecard()

    # ================================================================
    # 3. WEEKEND EFFECT
    # ================================================================
    print("\n" + "=" * 70)
    print("  STRATEGY 3: WEEKEND EFFECT (BTC momentum signal)")
    print("=" * 70)

    we_trades, we_matrix = reconstruct_weekend_trades()
    if we_trades:
        # N_trials: ~25 predictors x ~10 ensemble combos = ~250
        # But we're testing a simpler momentum variant here
        we_report = validate_strategy(
            trades=we_trades,
            n_trials=7,  # 7 lookback variants tested
            strategy_name="Weekend Effect (BTC momentum)",
            btc_returns=btc_returns,
            factor_returns=factors,
            returns_matrix=we_matrix if we_matrix.size > 0 else None,
            cpcv_groups=6,
        )
        we_report.print_scorecard()

    print("\n" + "=" * 70)
    print("  VALIDATION COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
