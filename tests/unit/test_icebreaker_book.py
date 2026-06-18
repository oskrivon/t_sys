"""Unit tests for L2 book reconstruction (Этап 1 — BookState)."""
from __future__ import annotations

import pytest

from src.icebreaker.book import BookState, Wall


def _seeded() -> BookState:
    b = BookState()
    b.apply_snapshot(
        bids=[(100.0, 5.0), (99.0, 10.0), (98.0, 2.0)],
        asks=[(101.0, 4.0), (102.0, 8.0)],
        update_id=1000,
    )
    return b


# ---------------------------------------------------------------------------
# Snapshot + basic views
# ---------------------------------------------------------------------------

def test_snapshot_sets_book_and_bbo():
    b = _seeded()
    assert b.seeded
    assert b.best_bid == 100.0
    assert b.best_ask == 101.0
    assert b.mid == 100.5
    assert b.spread == 1.0
    assert not b.is_crossed()


def test_snapshot_drops_zero_size_levels():
    b = BookState()
    b.apply_snapshot(bids=[(100.0, 0.0), (99.0, 3.0)], asks=[(101.0, 1.0)], update_id=1)
    assert 100.0 not in b.bids
    assert b.best_bid == 99.0


def test_empty_book_views_are_none():
    b = BookState()
    assert b.best_bid is None and b.best_ask is None
    assert b.mid is None and b.spread is None
    assert not b.is_crossed()


# ---------------------------------------------------------------------------
# Delta application
# ---------------------------------------------------------------------------

def test_delta_updates_and_inserts_levels():
    b = _seeded()
    ok = b.apply_delta(bids=[(100.0, 7.0), (97.0, 1.0)], asks=[], update_id=1001)
    assert ok
    assert b.bids[100.0] == 7.0   # updated
    assert b.bids[97.0] == 1.0    # inserted


def test_delta_zero_size_removes_level():
    b = _seeded()
    b.apply_delta(bids=[(100.0, 0.0)], asks=[], update_id=1001)
    assert 100.0 not in b.bids
    assert b.best_bid == 99.0


def test_delta_before_snapshot_rejected_and_flags_resync():
    b = BookState()
    ok = b.apply_delta(bids=[(100.0, 1.0)], asks=[], update_id=5)
    assert ok is False
    assert b.resync_needed is True
    assert b.rejected == 1


def test_stale_delta_rejected():
    b = _seeded()
    assert b.apply_delta(bids=[(100.0, 6.0)], asks=[], update_id=1000) is False  # <= last
    assert b.apply_delta(bids=[(100.0, 6.0)], asks=[], update_id=999) is False
    assert b.bids[100.0] == 5.0   # unchanged
    assert b.rejected == 2


def test_increasing_update_id_accepted():
    b = _seeded()
    assert b.apply_delta(bids=[(100.0, 6.0)], asks=[], update_id=1001) is True
    assert b.apply_delta(bids=[(100.0, 7.0)], asks=[], update_id=1002) is True
    assert b.last_update_id == 1002
    assert b.applied_deltas == 2


def test_resync_via_new_snapshot_clears_flag():
    b = BookState()
    b.apply_delta(bids=[(100.0, 1.0)], asks=[], update_id=5)  # rejected -> resync
    assert b.resync_needed
    b.apply_snapshot(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)], update_id=10)
    assert b.resync_needed is False
    assert b.seeded and b.best_bid == 100.0


def test_delta_without_update_ids_always_applies():
    """Some venues don't expose ids; deltas should still apply in order."""
    b = BookState()
    b.apply_snapshot(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
    assert b.apply_delta(bids=[(100.0, 2.0)], asks=[]) is True
    assert b.bids[100.0] == 2.0


# ---------------------------------------------------------------------------
# Crossed-book detection
# ---------------------------------------------------------------------------

def test_crossed_book_detected():
    b = BookState()
    b.apply_snapshot(bids=[(101.0, 1.0)], asks=[(100.0, 1.0)], update_id=1)
    assert b.is_crossed()


# ---------------------------------------------------------------------------
# top()
# ---------------------------------------------------------------------------

def test_top_n_ordering():
    b = _seeded()
    assert b.top(2, "bid") == [(100.0, 5.0), (99.0, 10.0)]
    assert b.top(2, "ask") == [(101.0, 4.0), (102.0, 8.0)]


def test_top_invalid_side_raises():
    with pytest.raises(ValueError):
        _seeded().top(1, "middle")


# ---------------------------------------------------------------------------
# walls() — the wall-eating primitive
# ---------------------------------------------------------------------------

def test_walls_threshold_and_sorting():
    b = BookState()
    b.apply_snapshot(
        bids=[(100.0, 1500.0)],   # 150k
        asks=[(200.0, 600.0),     # 120k
              (201.0, 100.0)],    # 20.1k — below
        update_id=1,
    )
    walls = b.walls(min_notional_usd=100_000)
    assert [(w.side, w.price) for w in walls] == [("bid", 100.0), ("ask", 200.0)]
    assert walls[0].notional == pytest.approx(150_000)


def test_walls_track_disappearance_after_delta():
    """The core event: a wall present, then eaten away below threshold."""
    b = BookState()
    b.apply_snapshot(bids=[(100.0, 1500.0)], asks=[(101.0, 1.0)], update_id=1)
    assert len(b.walls(100_000)) == 1
    # size collapses (absorbed) -> wall gone
    b.apply_delta(bids=[(100.0, 50.0)], asks=[], update_id=2)
    assert b.walls(100_000) == []


def test_wall_notional_property():
    assert Wall("bid", 100.0, 5.0).notional == 500.0
