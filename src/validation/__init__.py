"""Strategy validation pipeline.

Institutional-grade statistical validation for trading strategies.
Implements CPCV, PBO, DSR, factor decomposition, and regime analysis.

Quick start:
    from src.validation import validate_strategy
    report = validate_strategy(trades, n_trials=20)
    report.print_scorecard()
"""
from .scorecard import validate_strategy, ValidationReport

__all__ = ["validate_strategy", "ValidationReport"]
