"""Weekend ensemble configuration and predictor definitions."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator


class SignalType(str, Enum):
    """How to compute the predictor signal from daily OHLC data."""
    FRIDAY_RETURN = "friday_return"   # (Fri close - Fri open) / Fri open
    WEEK_RETURN = "week_return"       # (Fri close - Mon open) / Mon open


class PredictorDef(BaseModel):
    """Definition of a single predictor in the ensemble."""
    name: str
    ticker: str
    signal_type: SignalType
    description: str = ""
    invert: bool = False  # if True, positive return → bearish vote (e.g. USD/CNY)


class WeekendConfig(BaseModel):
    """Full configuration for the weekend ensemble strategy."""
    predictors: list[PredictorDef]
    majority_threshold: int = 3
    stop_loss_pct: float = 0.02
    entry_weekday: int = 4        # Friday
    entry_hour_utc: int = 21
    exit_weekday: int = 6         # Sunday
    exit_hour_utc: int = 12
    target_symbol: str = "BTC/USDT:USDT"
    db_path: str = "data/paper_trades.db"
    yfinance_retry_attempts: int = 3
    yfinance_retry_delay_seconds: float = 5.0

    # Live execution
    exchanges: list[str] = ["bybit", "binance"]
    notional_per_exchange: float = 0.0  # 0 = use full available balance
    leverage: int = 1
    margin_reserve_pct: float = 0.05  # keep 5% as buffer for fees/funding

    # Mid-weekend reversal: DEPRECATED, subsumed by V-bottom re-entry.
    # Kept for config backwards compatibility. No-op in runner.
    reversal_checkpoint_h: int = 24
    reversal_threshold_pct: float = 0.003

    # V-bottom re-entry: DISABLED with catastrophe SL 5%.
    # Was: SL 0.75% + bounce 0.5% + re-SL 1.0% → Sharpe 2.13.
    # Now: no SL improves compound CAGR +117% vs +73% (SL 2.5%), p=0.006.
    # Catastrophe SL 5% = safety net only, re-entry not needed.
    reentry_bounce_pct: float = 0.005     # kept for config compat, not used
    reentry_sl_pct: float = 0.01          # kept for config compat, not used
    reentry_max_hours: int = 16           # kept for config compat, not used

    @field_validator("majority_threshold")
    @classmethod
    def threshold_valid(cls, v: int, info) -> int:
        predictors = info.data.get("predictors")
        if predictors is not None and v > len(predictors):
            raise ValueError(
                f"majority_threshold ({v}) cannot exceed "
                f"number of predictors ({len(predictors)})"
            )
        if v < 1:
            raise ValueError("majority_threshold must be >= 1")
        return v

    @field_validator("stop_loss_pct")
    @classmethod
    def sl_valid(cls, v: float) -> float:
        if not 0 < v < 1:
            raise ValueError(f"stop_loss_pct must be between 0 and 1, got {v}")
        return v


# ── Default configuration: 5-WAY OOS-validated predictors ──────────────

DEFAULT_PREDICTORS = [
    PredictorDef(
        name="china_inet_fri",
        ticker="KWEB",
        signal_type=SignalType.FRIDAY_RETURN,
        description="China Internet ETF Friday return (best individual OOS, H2 Sharpe 1.34)",
    ),
    PredictorDef(
        name="japan_fri",
        ticker="EWJ",
        signal_type=SignalType.FRIDAY_RETURN,
        description="Japan ETF Friday return (H2 Sharpe 1.24)",
    ),
    PredictorDef(
        name="tech_week",
        ticker="XLK",
        signal_type=SignalType.WEEK_RETURN,
        description="Tech sector week return Mon->Fri (H2 Sharpe 0.94)",
    ),
    PredictorDef(
        name="energy_week",
        ticker="XLE",
        signal_type=SignalType.WEEK_RETURN,
        description="Energy sector week return Mon->Fri (H2 Sharpe 0.82)",
    ),
    PredictorDef(
        name="usdjpy_week",
        ticker="USDJPY=X",
        signal_type=SignalType.WEEK_RETURN,
        description="USD/JPY carry trade week return (H2 Sharpe 0.78)",
    ),
]

DEFAULT_CONFIG = WeekendConfig(
    predictors=DEFAULT_PREDICTORS,
    majority_threshold=3,
    stop_loss_pct=0.05,  # catastrophe-only SL (was 0.0075)
)
