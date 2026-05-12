"""Test the validation pipeline on synthetic + real data.

Usage:
    python scripts/research/test_validation_pipeline.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.backtest.models import Trade, Side, ExitReason, CostBreakdown
from src.validation.cpcv import cpcv_backtest
from src.validation.statistical import deflated_sharpe_from_returns, min_btl, pbo
from src.validation.factors import factor_decomposition, build_crypto_factors
from src.validation.regime import detect_regimes, regime_analysis
from src.validation.scorecard import validate_strategy


DATA = ROOT / "data" / "processed" / "candles"


# ======================================================================
# 1. Generate synthetic trades for testing
# ======================================================================

def generate_synthetic_trades(
    n_trades: int = 200,
    win_rate: float = 0.58,
    avg_win: float = 0.02,
    avg_loss: float = -0.015,
    seed: int = 42,
) -> list[Trade]:
    """Generate realistic-looking synthetic trades."""
    rng = np.random.RandomState(seed)
    trades = []
    start = datetime(2024, 1, 1)

    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "AAVE/USDT"]

    for i in range(n_trades):
        is_win = rng.random() < win_rate
        pnl = rng.normal(avg_win, 0.01) if is_win else rng.normal(avg_loss, 0.008)
        entry_time = start + timedelta(hours=i * 8 + rng.randint(0, 4))
        hold_hours = rng.exponential(24) + 1
        exit_time = entry_time + timedelta(hours=hold_hours)
        symbol = rng.choice(symbols)
        side = Side.LONG if rng.random() > 0.3 else Side.SHORT
        entry_price = 50000 + rng.normal(0, 5000) if "BTC" in symbol else 100 + rng.normal(0, 20)

        if side == Side.LONG:
            exit_price = entry_price * (1 + pnl)
        else:
            exit_price = entry_price * (1 - pnl)

        trades.append(Trade(
            symbol=symbol,
            side=side,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=abs(entry_price),
            exit_price=abs(exit_price),
            size_usd=1000.0,
            exit_reason=ExitReason.TP if is_win else ExitReason.SL,
            costs=CostBreakdown(entry_fee=0.00055, exit_fee=0.00055),
        ))

    return trades


# ======================================================================
# 2. Load real BTC data for factors + regimes
# ======================================================================

def load_btc_returns() -> pd.Series | None:
    btc_file = DATA / "BTCUSDT_1d.parquet"
    if not btc_file.exists():
        print("  [WARN] No BTC daily data found, skipping factors/regime")
        return None

    df = pd.read_parquet(btc_file)
    if "ts" in df.columns:
        df.index = pd.to_datetime(df["ts"], utc=True)
    elif "timestamp" in df.columns:
        df.index = pd.to_datetime(df["timestamp"], utc=True)
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)

    returns = df["close"].pct_change().dropna()
    returns.name = "btc_returns"
    return returns


def load_alt_returns() -> dict[str, pd.Series]:
    """Load daily returns for altcoins (for factor construction)."""
    alts = {}
    for symbol in ["ETHUSDT", "SOLUSDT", "DOGEUSDT", "LINKUSDT",
                    "AVAXUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT"]:
        f = DATA / f"{symbol}_1d.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        if "ts" in df.columns:
            df.index = pd.to_datetime(df["ts"], utc=True)
        elif "timestamp" in df.columns:
            df.index = pd.to_datetime(df["timestamp"], utc=True)
        elif not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        ret = df["close"].pct_change().dropna()
        alts[symbol] = ret
    return alts


# ======================================================================
# 3. Run tests
# ======================================================================

def test_cpcv():
    print("\n" + "="*50)
    print("TEST 1: CPCV")
    print("="*50)

    trades = generate_synthetic_trades(200, win_rate=0.58)
    result = cpcv_backtest(trades, n_groups=8, n_test_groups=2)
    print(result.summary())
    print(f"  Sharpe 5th percentile: {result.sharpe_5th:.2f}")
    print(f"  Sharpe 95th percentile: {np.percentile(result.sharpe_distribution, 95):.2f}")


def test_dsr():
    print("\n" + "="*50)
    print("TEST 2: Deflated Sharpe Ratio")
    print("="*50)

    rng = np.random.RandomState(42)
    # Simulate daily returns with edge
    returns = rng.normal(0.001, 0.02, 500)

    for n_trials in [1, 5, 20, 100]:
        dsr = deflated_sharpe_from_returns(returns, n_trials=n_trials)
        status = "PASS" if dsr.is_significant else "FAIL"
        print(
            f"  N={n_trials:>3} trials: "
            f"Sharpe={dsr.observed_sharpe:.2f}  "
            f"E[max]={dsr.expected_max_sharpe:.2f}  "
            f"p={dsr.p_value:.3f}  [{status}]"
        )


def test_min_btl():
    print("\n" + "="*50)
    print("TEST 3: Minimum Backtest Length")
    print("="*50)

    for n_trials in [1, 5, 20, 50, 100]:
        result = min_btl(n_trials=n_trials, actual_years=2.0)
        print(f"  {result.summary()}")


def test_pbo():
    print("\n" + "="*50)
    print("TEST 4: PBO")
    print("="*50)

    rng = np.random.RandomState(42)

    # Case 1: real edge (all strategies have positive mean)
    real_edge = rng.normal(0.001, 0.02, (500, 10))
    result1 = pbo(real_edge, n_subsets=8)
    print(f"  Real edge (10 strategies, all mean>0): PBO={result1.pbo:.2f} {'OVERFIT' if result1.is_overfit else 'OK'}")

    # Case 2: no edge (pure noise)
    noise = rng.normal(0, 0.02, (500, 50))
    result2 = pbo(noise, n_subsets=8)
    print(f"  Pure noise (50 strategies, mean=0):    PBO={result2.pbo:.2f} {'OVERFIT' if result2.is_overfit else 'OK'}")


def test_factors():
    print("\n" + "="*50)
    print("TEST 5: Factor Decomposition")
    print("="*50)

    btc_returns = load_btc_returns()
    if btc_returns is None:
        print("  Skipped (no data)")
        return

    alt_returns = load_alt_returns()
    print(f"  Loaded BTC ({len(btc_returns)} days) + {len(alt_returns)} alts")

    factors = build_crypto_factors(
        btc_returns=btc_returns,
        alt_returns=alt_returns if len(alt_returns) >= 4 else None,
    )
    print(f"  Factors built: {list(factors.columns)}")
    print(f"  Date range: {factors.index[0]} -- {factors.index[-1]}")

    # Create a synthetic strategy that is partially correlated with BTC
    rng = np.random.RandomState(42)
    n = len(factors)
    strategy_returns = 0.3 * factors["CMKT"].values + rng.normal(0.0005, 0.01, n)
    strategy_series = pd.Series(strategy_returns, index=factors.index)

    result = factor_decomposition(strategy_series, factors)
    print(result.summary())


def test_regimes():
    print("\n" + "="*50)
    print("TEST 6: Regime Detection")
    print("="*50)

    btc_returns = load_btc_returns()
    if btc_returns is None:
        print("  Skipped (no data)")
        return

    regimes = detect_regimes(btc_returns, n_regimes=2)
    print(regimes.summary())

    # Regime analysis with synthetic strategy
    rng = np.random.RandomState(42)
    strategy_returns = pd.Series(
        rng.normal(0.0005, 0.015, len(btc_returns)),
        index=btc_returns.index,
    )

    analysis = regime_analysis(strategy_returns, regimes)
    print(analysis.summary())


def test_full_scorecard():
    print("\n" + "="*50)
    print("TEST 7: Full Validation Scorecard")
    print("="*50)

    trades = generate_synthetic_trades(200, win_rate=0.58)
    btc_returns = load_btc_returns()

    # Build factors if we have data
    factor_returns = None
    if btc_returns is not None:
        alt_returns = load_alt_returns()
        if len(alt_returns) >= 4:
            factor_returns = build_crypto_factors(btc_returns, alt_returns)

    report = validate_strategy(
        trades=trades,
        n_trials=5,
        strategy_name="Synthetic Test (WR=58%)",
        btc_returns=btc_returns,
        factor_returns=factor_returns,
    )
    report.print_scorecard()


# ======================================================================
# Main
# ======================================================================

if __name__ == "__main__":
    print("VALIDATION PIPELINE TEST")
    print("=" * 50)

    test_cpcv()
    test_dsr()
    test_min_btl()
    test_pbo()
    test_factors()
    test_regimes()
    test_full_scorecard()

    print("\nAll tests completed OK")
