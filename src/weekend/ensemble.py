"""Ensemble vote computation — pure logic, zero I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.weekend.config import WeekendConfig


@dataclass(frozen=True)
class PredictorResult:
    """Result of computing one predictor."""
    name: str
    ticker: str
    value_pct: float
    vote: int  # +1 (bullish) or -1 (bearish)


@dataclass(frozen=True)
class EnsembleResult:
    """Result of the ensemble vote."""
    date: str
    predictors: tuple[PredictorResult, ...]
    failed_predictors: tuple[str, ...]
    vote_sum: int
    total_votes: int
    direction: Optional[str]  # "long", "short", or None
    consensus: int
    confidence: float  # consensus / total_votes, 0 if no votes


def compute_ensemble(
    results: list[PredictorResult | None],
    predictor_names: list[str],
    majority_threshold: int,
    date: str,
) -> EnsembleResult:
    """Compute ensemble vote from individual predictor results.

    Args:
        results: One result per predictor (None = failed to fetch).
        predictor_names: Names of all predictors (parallel to results).
        majority_threshold: Minimum votes in same direction for a signal.
        date: The Friday date string (YYYY-MM-DD).

    Returns:
        EnsembleResult with direction=None if no consensus.
    """
    successful = []
    failed = []

    for name, result in zip(predictor_names, results):
        if result is None:
            failed.append(name)
        else:
            successful.append(result)

    total_votes = len(successful)
    vote_sum = sum(r.vote for r in successful)

    # Cannot reach majority if not enough predictors succeeded
    if total_votes < majority_threshold:
        return EnsembleResult(
            date=date,
            predictors=tuple(successful),
            failed_predictors=tuple(failed),
            vote_sum=vote_sum,
            total_votes=total_votes,
            direction=None,
            consensus=0,
            confidence=0.0,
        )

    bullish_count = (total_votes + vote_sum) // 2
    bearish_count = (total_votes - vote_sum) // 2

    if bullish_count >= majority_threshold:
        direction = "long"
        consensus = bullish_count
    elif bearish_count >= majority_threshold:
        direction = "short"
        consensus = bearish_count
    else:
        direction = None
        consensus = 0

    confidence = consensus / total_votes if total_votes > 0 else 0.0

    return EnsembleResult(
        date=date,
        predictors=tuple(successful),
        failed_predictors=tuple(failed),
        vote_sum=vote_sum,
        total_votes=total_votes,
        direction=direction,
        consensus=consensus,
        confidence=confidence,
    )


def compute_sl_price(entry_price: float, direction: str, sl_pct: float) -> float:
    """Compute stop-loss price."""
    if direction == "long":
        return entry_price * (1 - sl_pct)
    return entry_price * (1 + sl_pct)


def compute_pnl(entry_price: float, exit_price: float, direction: str) -> float:
    """Compute P&L percentage."""
    if direction == "long":
        return (exit_price - entry_price) / entry_price * 100
    return (entry_price - exit_price) / entry_price * 100


def is_sl_hit(current_price: float, sl_price: float, direction: str) -> bool:
    """Check if stop-loss has been hit."""
    if direction == "long":
        return current_price <= sl_price
    return current_price >= sl_price
