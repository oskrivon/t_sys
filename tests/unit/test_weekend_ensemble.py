"""Unit tests for weekend ensemble vote logic."""
from __future__ import annotations

import pytest

from src.weekend.ensemble import (
    EnsembleResult,
    PredictorResult,
    compute_ensemble,
    compute_pnl,
    compute_sl_price,
    is_sl_hit,
)


def _pr(name: str, value: float, vote: int) -> PredictorResult:
    """Shorthand for creating a PredictorResult."""
    return PredictorResult(name=name, ticker=f"T_{name}", value_pct=value, vote=vote)


NAMES_5 = ["p1", "p2", "p3", "p4", "p5"]
DATE = "2026-04-25"


class TestComputeEnsemble:
    """Tests for the ensemble vote aggregation."""

    def test_3_bullish_2_bearish_is_long(self):
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1), _pr("p3", 0.1, 1),
                    _pr("p4", -0.2, -1), _pr("p5", -0.4, -1)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction == "long"
        assert ens.vote_sum == 1  # 3 - 2
        assert ens.consensus == 3
        assert ens.total_votes == 5

    def test_2_bullish_3_bearish_is_short(self):
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1), _pr("p3", -0.1, -1),
                    _pr("p4", -0.2, -1), _pr("p5", -0.4, -1)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction == "short"
        assert ens.vote_sum == -1
        assert ens.consensus == 3

    def test_5_unanimous_bullish(self):
        results = [_pr(f"p{i}", 0.5, 1) for i in range(1, 6)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction == "long"
        assert ens.vote_sum == 5
        assert ens.consensus == 5
        assert ens.confidence == 1.0

    def test_5_unanimous_bearish(self):
        results = [_pr(f"p{i}", -0.5, -1) for i in range(1, 6)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction == "short"
        assert ens.vote_sum == -5
        assert ens.consensus == 5
        assert ens.confidence == 1.0

    def test_2_vs_2_plus_1_failed_no_trade(self):
        """2 bullish + 2 bearish + 1 failed = 4 votes, sum=0, no majority."""
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1),
                    _pr("p3", -0.1, -1), _pr("p4", -0.2, -1), None]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction is None
        assert ens.total_votes == 4
        assert len(ens.failed_predictors) == 1

    def test_all_failed_no_trade(self):
        results = [None, None, None, None, None]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction is None
        assert ens.total_votes == 0
        assert len(ens.failed_predictors) == 5
        assert ens.confidence == 0.0

    def test_exactly_3_succeed_all_agree_trade(self):
        """3 predictors succeed, all bullish = LONG (meets threshold)."""
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1), _pr("p3", 0.1, 1),
                    None, None]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction == "long"
        assert ens.consensus == 3
        assert ens.total_votes == 3

    def test_exactly_3_succeed_2_vs_1_no_trade(self):
        """3 predictors succeed, 2 bullish + 1 bearish = sum=1, below threshold=3."""
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1), _pr("p3", -0.1, -1),
                    None, None]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction is None

    def test_2_succeed_below_threshold_no_trade(self):
        """Only 2 predictors succeed — can never reach threshold=3."""
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1), None, None, None]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction is None
        assert ens.total_votes == 2

    def test_zero_return_is_bearish_vote(self):
        """A zero return predictor has vote=-1 (bearish), affects ensemble."""
        # 2 bullish + 3 bearish (including zero-return) = SHORT
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1),
                    _pr("p3", 0.0, -1), _pr("p4", -0.1, -1), _pr("p5", -0.2, -1)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.direction == "short"

    def test_confidence_calculation(self):
        """confidence = consensus / total_votes."""
        results = [_pr("p1", 0.5, 1), _pr("p2", 0.3, 1), _pr("p3", 0.1, 1),
                    _pr("p4", -0.2, -1), _pr("p5", -0.4, -1)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert ens.confidence == pytest.approx(3 / 5)

    def test_failed_predictors_tracked(self):
        results = [_pr("p1", 0.5, 1), None, _pr("p3", 0.1, 1), None, _pr("p5", 0.2, 1)]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert set(ens.failed_predictors) == {"p2", "p4"}
        assert ens.direction == "long"

    def test_date_preserved(self):
        results = [_pr("p1", 0.5, 1)] * 5
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date="2026-12-25")
        assert ens.date == "2026-12-25"

    def test_threshold_1_always_trades(self):
        """With threshold=1, any single predictor triggers a trade."""
        results = [_pr("p1", 0.5, 1), None, None, None, None]
        ens = compute_ensemble(results, NAMES_5, majority_threshold=1, date=DATE)
        assert ens.direction == "long"

    def test_predictors_immutable(self):
        """EnsembleResult stores tuples, not lists."""
        results = [_pr("p1", 0.5, 1)] * 5
        ens = compute_ensemble(results, NAMES_5, majority_threshold=3, date=DATE)
        assert isinstance(ens.predictors, tuple)
        assert isinstance(ens.failed_predictors, tuple)


class TestComputeSlPrice:
    def test_long_sl_below_entry(self):
        sl = compute_sl_price(100_000.0, "long", 0.02)
        assert sl == pytest.approx(98_000.0)

    def test_short_sl_above_entry(self):
        sl = compute_sl_price(100_000.0, "short", 0.02)
        assert sl == pytest.approx(102_000.0)

    def test_small_sl_pct(self):
        sl = compute_sl_price(50_000.0, "long", 0.005)
        assert sl == pytest.approx(49_750.0)


class TestComputePnl:
    def test_long_profit(self):
        pnl = compute_pnl(100_000.0, 101_000.0, "long")
        assert pnl == pytest.approx(1.0)

    def test_long_loss(self):
        pnl = compute_pnl(100_000.0, 98_000.0, "long")
        assert pnl == pytest.approx(-2.0)

    def test_short_profit(self):
        pnl = compute_pnl(100_000.0, 99_000.0, "short")
        assert pnl == pytest.approx(1.0)

    def test_short_loss(self):
        pnl = compute_pnl(100_000.0, 102_000.0, "short")
        assert pnl == pytest.approx(-2.0)

    def test_zero_move(self):
        pnl = compute_pnl(50_000.0, 50_000.0, "long")
        assert pnl == pytest.approx(0.0)


class TestIsSlHit:
    def test_long_sl_hit(self):
        assert is_sl_hit(97_999.0, 98_000.0, "long") is True

    def test_long_sl_exact(self):
        assert is_sl_hit(98_000.0, 98_000.0, "long") is True

    def test_long_sl_not_hit(self):
        assert is_sl_hit(98_001.0, 98_000.0, "long") is False

    def test_short_sl_hit(self):
        assert is_sl_hit(102_001.0, 102_000.0, "short") is True

    def test_short_sl_exact(self):
        assert is_sl_hit(102_000.0, 102_000.0, "short") is True

    def test_short_sl_not_hit(self):
        assert is_sl_hit(101_999.0, 102_000.0, "short") is False
