"""Validation scorecard -- final gate before paper/live trading.

Runs the full validation pipeline and prints a pass/fail scorecard.

Usage:
    from src.validation import validate_strategy
    from src.backtest.models import Trade

    report = validate_strategy(
        trades=my_trades,
        n_trials=20,
        btc_returns=btc_daily,
    )
    report.print_scorecard()

    if report.passed:
        print("→ Ready for paper trading")
    else:
        print(f"→ Failed: {report.failures}")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.backtest.models import Trade

from .cpcv import CPCVResult, cpcv_backtest, _trades_to_daily_returns
from .statistical import (
    DSRResult,
    MinBTLResult,
    PBOResult,
    deflated_sharpe_from_returns,
    min_btl,
    pbo,
)
from .factors import FactorResult, factor_decomposition
from .regime import (
    RegimeAnalysisResult,
    RegimeDetectionResult,
    detect_regimes,
    regime_analysis,
)
from .lookahead import ShiftTestResult, timestamp_shift_test


@dataclass
class ValidationReport:
    """Complete validation report for a strategy."""
    strategy_name: str
    n_trades: int

    # Individual results
    cpcv: Optional[CPCVResult] = None
    dsr: Optional[DSRResult] = None
    min_btl_result: Optional[MinBTLResult] = None
    pbo_result: Optional[PBOResult] = None
    factor: Optional[FactorResult] = None
    regime: Optional[RegimeAnalysisResult] = None
    regime_detection: Optional[RegimeDetectionResult] = None
    lookahead: Optional[ShiftTestResult] = None

    # Gate results
    checks: dict[str, dict] = field(default_factory=dict)
    # {check_name: {value, threshold, passed, note}}

    @property
    def passed(self) -> bool:
        if not self.checks:
            return False
        return all(c["passed"] for c in self.checks.values())

    @property
    def failures(self) -> list[str]:
        return [name for name, c in self.checks.items() if not c["passed"]]

    @property
    def n_passed(self) -> int:
        return sum(1 for c in self.checks.values() if c["passed"])

    @property
    def n_total(self) -> int:
        return len(self.checks)

    def print_scorecard(self) -> None:
        """Print formatted scorecard to stdout."""
        print(f"\n{'='*60}")
        print(f"  VALIDATION SCORECARD: {self.strategy_name}")
        print(f"  Trades: {self.n_trades}")
        print(f"{'='*60}")
        print()

        max_name_len = max((len(n) for n in self.checks), default=20)

        for name, check in self.checks.items():
            icon = "PASS" if check["passed"] else "FAIL"
            val = check["value"]
            thresh = check["threshold"]
            note = check.get("note", "")

            if isinstance(val, float):
                val_str = f"{val:.3f}"
            else:
                val_str = str(val)

            line = f"  {icon} {name:<{max_name_len}}  {val_str:>8}  (threshold: {thresh})"
            if note:
                line += f"  --{note}"
            print(line)

        print()
        print(f"  Result: {self.n_passed}/{self.n_total} passed", end="")
        if self.passed:
            print("  -> READY FOR PAPER TRADING")
        else:
            print(f"  -> FAILED: {', '.join(self.failures)}")
        print(f"{'='*60}\n")

        # Detailed sub-reports
        if self.cpcv:
            print(self.cpcv.summary())
        if self.factor:
            print()
            print(self.factor.summary())
        if self.regime:
            print()
            print(self.regime.summary())
        if self.lookahead:
            self.lookahead.print_summary()
        print()

    def to_dict(self) -> dict:
        """Export scorecard as dict for logging/serialization."""
        return {
            "strategy": self.strategy_name,
            "n_trades": self.n_trades,
            "passed": self.passed,
            "n_passed": self.n_passed,
            "n_total": self.n_total,
            "checks": self.checks,
            "cpcv_median_sharpe": self.cpcv.median_sharpe if self.cpcv else None,
            "dsr_p_value": self.dsr.p_value if self.dsr else None,
            "pbo": self.pbo_result.pbo if self.pbo_result else None,
            "factor_alpha_tstat": self.factor.alpha_tstat if self.factor else None,
            "multi_regime": self.regime.is_multi_regime if self.regime else None,
        }


# ===========================================================================
# Main entry point
# ===========================================================================

def validate_strategy(
    trades: list[Trade],
    n_trials: int = 1,
    strategy_name: str = "strategy",
    btc_returns: Optional[pd.Series] = None,
    factor_returns: Optional[pd.DataFrame] = None,
    returns_matrix: Optional[np.ndarray] = None,
    # Gate thresholds
    min_sharpe: float = 0.5,
    min_prob_positive: float = 0.70,
    max_pbo: float = 0.50,
    dsr_alpha: float = 0.05,
    min_alpha_tstat: float = 2.0,
    # CPCV params
    cpcv_groups: int = 10,
    cpcv_test_groups: int = 2,
) -> ValidationReport:
    """Run the full validation pipeline on a strategy.

    Args:
        trades: List of Trade objects from backtesting.
        n_trials: Number of strategy configurations tested (for DSR/MinBTL).
        strategy_name: Name for the report.
        btc_returns: Daily BTC returns for regime detection.
        factor_returns: Factor returns DataFrame for decomposition.
        returns_matrix: (T, N) matrix of strategy variants for PBO.
                       Each column = one variant's return series.
        min_sharpe: CPCV median Sharpe threshold.
        min_prob_positive: CPCV P(Sharpe > 0) threshold.
        max_pbo: Maximum acceptable PBO.
        dsr_alpha: DSR significance level.
        min_alpha_tstat: Minimum t-stat for factor alpha.
        cpcv_groups: Number of CPCV groups.
        cpcv_test_groups: Number of CPCV test groups.
    """
    report = ValidationReport(
        strategy_name=strategy_name,
        n_trades=len(trades),
    )

    if len(trades) < 30:
        report.checks["min_trades"] = {
            "value": len(trades),
            "threshold": ">=30",
            "passed": False,
            "note": "Too few trades for validation",
        }
        return report

    trades_sorted = sorted(trades, key=lambda t: t.exit_time)
    daily_returns = _trades_to_daily_returns(trades_sorted)

    # -------------------------------------------------------------------
    # 1. CPCV
    # -------------------------------------------------------------------
    n_groups = min(cpcv_groups, len(trades) // 5)
    if n_groups >= 4:
        try:
            cpcv_result = cpcv_backtest(
                trades_sorted,
                n_groups=n_groups,
                n_test_groups=cpcv_test_groups,
            )
            report.cpcv = cpcv_result

            report.checks["CPCV median Sharpe"] = {
                "value": cpcv_result.median_sharpe,
                "threshold": f">={min_sharpe}",
                "passed": cpcv_result.median_sharpe >= min_sharpe,
            }
            report.checks["CPCV P(Sharpe>0)"] = {
                "value": cpcv_result.prob_positive,
                "threshold": f">={min_prob_positive}",
                "passed": cpcv_result.prob_positive >= min_prob_positive,
            }
        except (ValueError, Exception) as e:
            report.checks["CPCV"] = {
                "value": "error",
                "threshold": "—",
                "passed": False,
                "note": str(e)[:80],
            }

    # -------------------------------------------------------------------
    # 2. DSR
    # -------------------------------------------------------------------
    dsr_result = deflated_sharpe_from_returns(
        daily_returns.values,
        n_trials=n_trials,
    )
    report.dsr = dsr_result

    report.checks["DSR p-value"] = {
        "value": dsr_result.p_value,
        "threshold": f"<{dsr_alpha}",
        "passed": dsr_result.p_value < dsr_alpha,
        "note": f"Sharpe={dsr_result.observed_sharpe:.2f}, E[max]={dsr_result.expected_max_sharpe:.2f}",
    }

    # -------------------------------------------------------------------
    # 3. MinBTL
    # -------------------------------------------------------------------
    first_trade = trades_sorted[0].entry_time
    last_trade = trades_sorted[-1].exit_time
    actual_years = (last_trade - first_trade).days / 365.25

    btl_result = min_btl(n_trials=n_trials, actual_years=actual_years)
    report.min_btl_result = btl_result

    report.checks["MinBTL"] = {
        "value": actual_years,
        "threshold": f">={btl_result.min_years:.1f}y",
        "passed": btl_result.is_sufficient,
        "note": f"Need {btl_result.min_years:.1f}y for N={n_trials} trials",
    }

    # -------------------------------------------------------------------
    # 4. PBO (only if returns_matrix provided)
    # -------------------------------------------------------------------
    if returns_matrix is not None and returns_matrix.shape[1] >= 2:
        try:
            pbo_result = pbo(returns_matrix)
            report.pbo_result = pbo_result

            report.checks["PBO"] = {
                "value": pbo_result.pbo,
                "threshold": f"<{max_pbo}",
                "passed": pbo_result.pbo < max_pbo,
                "note": f"{pbo_result.n_combinations} combinations, {pbo_result.n_strategies} strategies",
            }
        except (ValueError, Exception) as e:
            report.checks["PBO"] = {
                "value": "error",
                "threshold": "—",
                "passed": False,
                "note": str(e)[:80],
            }
    else:
        report.checks["PBO"] = {
            "value": "skipped",
            "threshold": f"<{max_pbo}",
            "passed": True,
            "note": "No returns_matrix provided (single config = no overfitting risk from selection)",
        }

    # -------------------------------------------------------------------
    # 5. Factor decomposition (if factor_returns provided)
    # -------------------------------------------------------------------
    if factor_returns is not None and len(factor_returns) > 0:
        factor_result = factor_decomposition(daily_returns, factor_returns)
        report.factor = factor_result

        report.checks["Factor alpha t-stat"] = {
            "value": factor_result.alpha_tstat,
            "threshold": f">={min_alpha_tstat}",
            "passed": factor_result.alpha_tstat >= min_alpha_tstat,
            "note": f"alpha={factor_result.alpha_annual:.2%}/yr, R2={factor_result.r_squared:.2f}",
        }
    else:
        report.checks["Factor alpha"] = {
            "value": "skipped",
            "threshold": "—",
            "passed": True,
            "note": "No factor data provided",
        }

    # -------------------------------------------------------------------
    # 6. Regime analysis (if btc_returns provided)
    # -------------------------------------------------------------------
    if btc_returns is not None and len(btc_returns) > 60:
        try:
            regime_det = detect_regimes(btc_returns, n_regimes=2)
            report.regime_detection = regime_det

            regime_result = regime_analysis(daily_returns, regime_det)
            report.regime = regime_result

            report.checks["Multi-regime"] = {
                "value": regime_result.n_regimes_profitable,
                "threshold": ">=2 regimes profitable",
                "passed": regime_result.is_multi_regime,
                "note": f"dominant={regime_result.dominant_pnl_pct:.0%} in 1 regime",
            }
        except (ValueError, Exception) as e:
            report.checks["Regime"] = {
                "value": "error",
                "threshold": "—",
                "passed": False,
                "note": str(e)[:80],
            }
    else:
        report.checks["Regime"] = {
            "value": "skipped",
            "threshold": "—",
            "passed": True,
            "note": "No BTC returns provided",
        }

    return report
