"""Look-ahead bias detection via timestamp shift test.

Principle: if a filter/feature uses look-ahead data, shifting its timestamps
backward should degrade performance significantly. If shifting by 1 day
cuts Sharpe by >50%, the feature likely contains look-ahead bias.

Usage:
    result = timestamp_shift_test(
        trade_returns=returns_series,
        trade_timestamps=timestamps,
        feature_series=nq_daily_returns,
        feature_name="NQ",
        filter_fn=lambda feat_val: feat_val >= -0.5,  # skip if NQ < -0.5%
    )
    result.print_summary()
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Callable, Optional

import numpy as np
import pandas as pd


@dataclass
class ShiftTestResult:
    """Result of timestamp shift look-ahead test."""
    feature_name: str
    sharpe_by_shift: dict[int, float]  # shift_days → Sharpe
    n_trades_by_shift: dict[int, int]
    ratio_0_vs_minus1: float  # Sharpe(shift=0) / Sharpe(shift=-1)
    has_lookahead: bool  # True if ratio > threshold
    threshold: float

    def print_summary(self) -> None:
        print(f"\n  Timestamp Shift Test: {self.feature_name}")
        print(f"  {'Shift':>8} {'N':>5} {'Sharpe':>8}")
        for shift in sorted(self.sharpe_by_shift.keys()):
            n = self.n_trades_by_shift.get(shift, 0)
            sh = self.sharpe_by_shift[shift]
            marker = " ← look-ahead" if shift == 0 else (" ← honest" if shift == -1 else "")
            print(f"  {shift:>+6}d {n:>5} {sh:>+7.2f}{marker}")
        status = "WARNING: look-ahead detected" if self.has_lookahead else "OK: no look-ahead"
        print(f"  Ratio shift0/shift-1: {self.ratio_0_vs_minus1:.2f} "
              f"(threshold: {self.threshold:.1f}) → {status}")


def timestamp_shift_test(
    trade_pnls: np.ndarray,
    trade_timestamps: list[pd.Timestamp],
    feature_series: pd.Series,
    feature_name: str = "feature",
    filter_fn: Callable[[float], bool] = lambda x: x >= -0.5,
    shifts: list[int] | None = None,
    lookahead_threshold: float = 1.5,
    years: float | None = None,
) -> ShiftTestResult:
    """Run timestamp shift test on a feature used for trade filtering.

    Args:
        trade_pnls: PnL array for all trades (before feature filter).
        trade_timestamps: Timestamp for each trade entry.
        feature_series: Daily feature values (e.g. NQ returns), DatetimeIndex.
        feature_name: Name for reporting.
        filter_fn: Function(feature_value) → True to keep trade, False to skip.
                   Default: keep if feature >= -0.5%.
        shifts: List of day shifts to test. Default: [0, -1, -2, -3, -5, -7].
        lookahead_threshold: Ratio of Sharpe(0)/Sharpe(-1) above which
                            look-ahead is flagged.
        years: Timespan in years for Sharpe annualization.

    Returns:
        ShiftTestResult with per-shift Sharpe and look-ahead flag.
    """
    if shifts is None:
        shifts = [0, -1, -2, -3, -5, -7]

    if years is None:
        ts_arr = pd.DatetimeIndex(trade_timestamps)
        years = (ts_arr.max() - ts_arr.min()).days / 365.25
        if years <= 0:
            years = 1.0

    sharpe_by_shift = {}
    n_by_shift = {}

    for shift in shifts:
        kept_pnls = []
        for pnl, ts in zip(trade_pnls, trade_timestamps):
            shifted_date = (ts + pd.Timedelta(days=shift)).strftime("%Y-%m-%d")
            feat_row = feature_series[feature_series.index <= pd.Timestamp(shifted_date)]
            if len(feat_row) == 0:
                kept_pnls.append(pnl)  # no data → keep trade
                continue
            feat_val = feat_row.iloc[-1]
            if np.isnan(feat_val) or filter_fn(feat_val):
                kept_pnls.append(pnl)

        arr = np.array(kept_pnls)
        n_by_shift[shift] = len(arr)
        if len(arr) > 5 and np.std(arr) > 0:
            sharpe_by_shift[shift] = float(
                arr.mean() / arr.std() * sqrt(len(arr) / years)
            )
        else:
            sharpe_by_shift[shift] = 0.0

    # Compute ratio
    sh0 = sharpe_by_shift.get(0, 0)
    sh1 = sharpe_by_shift.get(-1, 0)
    ratio = sh0 / sh1 if sh1 > 0.1 else (10.0 if sh0 > 0.1 else 1.0)

    return ShiftTestResult(
        feature_name=feature_name,
        sharpe_by_shift=sharpe_by_shift,
        n_trades_by_shift=n_by_shift,
        ratio_0_vs_minus1=ratio,
        has_lookahead=ratio > lookahead_threshold,
        threshold=lookahead_threshold,
    )
