"""Property-based tests for weekend ensemble invariants."""
from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, assume

from src.weekend.ensemble import (
    PredictorResult,
    compute_ensemble,
    compute_pnl,
    compute_sl_price,
    is_sl_hit,
)


# ── Strategies ─────────────────────────────────────────────────────────

votes = st.sampled_from([-1, 1])
returns = st.floats(min_value=-50.0, max_value=50.0, allow_nan=False)
prices = st.floats(min_value=100.0, max_value=200000.0, allow_nan=False)
sl_pcts = st.floats(min_value=0.001, max_value=0.5, allow_nan=False)
directions = st.sampled_from(["long", "short"])


def predictor_results(n: int):
    """Generate a list of n PredictorResult | None."""
    return st.lists(
        st.one_of(
            st.builds(
                PredictorResult,
                name=st.text(min_size=1, max_size=5),
                ticker=st.text(min_size=1, max_size=5),
                value_pct=returns,
                vote=votes,
            ),
            st.none(),
        ),
        min_size=n, max_size=n,
    )


# ── Ensemble Vote Properties ──────────────────────────────────────────

@given(
    results=predictor_results(5),
    threshold=st.integers(min_value=1, max_value=5),
)
def test_vote_sum_in_range(results, threshold):
    """vote_sum is always between -N and N where N = successful predictors."""
    names = [f"p{i}" for i in range(5)]
    ens = compute_ensemble(results, names, threshold, "2026-01-01")
    assert -ens.total_votes <= ens.vote_sum <= ens.total_votes


@given(
    results=predictor_results(5),
    threshold=st.integers(min_value=1, max_value=5),
)
def test_direction_consistent_with_votes(results, threshold):
    """If direction is LONG, there are enough bullish votes; SHORT → enough bearish."""
    names = [f"p{i}" for i in range(5)]
    ens = compute_ensemble(results, names, threshold, "2026-01-01")

    if ens.direction == "long":
        bullish = (ens.total_votes + ens.vote_sum) // 2
        assert bullish >= threshold
    elif ens.direction == "short":
        bearish = (ens.total_votes - ens.vote_sum) // 2
        assert bearish >= threshold
    else:
        # No direction: neither side has enough votes
        bullish = (ens.total_votes + ens.vote_sum) // 2
        bearish = (ens.total_votes - ens.vote_sum) // 2
        assert bullish < threshold and bearish < threshold


@given(
    results=predictor_results(5),
    threshold=st.integers(min_value=1, max_value=5),
)
def test_consensus_bounded(results, threshold):
    """consensus is always <= total_votes."""
    names = [f"p{i}" for i in range(5)]
    ens = compute_ensemble(results, names, threshold, "2026-01-01")
    assert ens.consensus <= ens.total_votes


@given(
    results=predictor_results(5),
    threshold=st.integers(min_value=1, max_value=5),
)
def test_deterministic(results, threshold):
    """Same inputs always produce same output."""
    names = [f"p{i}" for i in range(5)]
    ens1 = compute_ensemble(results, names, threshold, "2026-01-01")
    ens2 = compute_ensemble(results, names, threshold, "2026-01-01")
    assert ens1 == ens2


# ── SL Price Properties ───────────────────────────────────────────────

@given(entry=prices, sl_pct=sl_pcts, direction=directions)
def test_sl_on_losing_side(entry, sl_pct, direction):
    """SL is always on the side where the trade loses money."""
    sl = compute_sl_price(entry, direction, sl_pct)
    if direction == "long":
        assert sl < entry
    else:
        assert sl > entry


# ── P&L Properties ────────────────────────────────────────────────────

@given(entry=prices, exit_=prices, direction=directions)
def test_pnl_sign_matches_direction(entry, exit_, direction):
    """LONG: exit>entry = profit; SHORT: exit<entry = profit."""
    assume(entry != exit_)
    pnl = compute_pnl(entry, exit_, direction)
    if direction == "long":
        assert (pnl > 0) == (exit_ > entry)
    else:
        assert (pnl > 0) == (exit_ < entry)


@given(entry=prices, direction=directions)
def test_pnl_zero_on_flat(entry, direction):
    """If exit == entry, P&L is zero."""
    pnl = compute_pnl(entry, entry, direction)
    assert pnl == 0.0
