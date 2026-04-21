"""Tests for src/execution/position_tracker.py — in-memory position tracking."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.execution.position_tracker import LivePosition, PositionTracker


# ---------------------------------------------------------------------------
# LivePosition properties
# ---------------------------------------------------------------------------

class TestLivePosition:
    def test_notional(self, sample_long_position):
        assert sample_long_position.notional == Decimal("0.001") * Decimal("50000")

    def test_margin_with_leverage(self, sample_long_position):
        # leverage=10 → margin = notional / 10
        expected = sample_long_position.notional / 10
        assert sample_long_position.margin == expected

    def test_margin_leverage_1(self):
        pos = LivePosition(
            symbol="SOL/USDT:USDT",
            side="long",
            qty=Decimal("1"),
            entry_price=Decimal("100"),
            leverage=1,
        )
        assert pos.margin == pos.notional

    def test_margin_leverage_zero_returns_notional(self):
        """leverage=0 is an edge case — should return notional (no division)."""
        pos = LivePosition(
            symbol="SOL/USDT:USDT",
            side="long",
            qty=Decimal("10"),
            entry_price=Decimal("100"),
            leverage=0,
        )
        assert pos.margin == pos.notional


# ---------------------------------------------------------------------------
# PositionTracker lifecycle
# ---------------------------------------------------------------------------

class TestPositionTrackerLifecycle:
    def test_open_and_get(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        assert position_tracker.get(sample_long_position.symbol) is sample_long_position

    def test_close_returns_position(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        closed = position_tracker.close(sample_long_position.symbol)
        assert closed is sample_long_position
        assert position_tracker.get(sample_long_position.symbol) is None

    def test_close_nonexistent_returns_none(self, position_tracker):
        assert position_tracker.close("DOGE/USDT:USDT") is None

    def test_has_position_true(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        assert position_tracker.has_position(sample_long_position.symbol) is True

    def test_has_position_false(self, position_tracker):
        assert position_tracker.has_position("NONEXIST/USDT:USDT") is False

    def test_count_tracks_correctly(
        self, position_tracker, sample_long_position, sample_short_position
    ):
        assert position_tracker.count == 0
        position_tracker.open(sample_long_position)
        assert position_tracker.count == 1
        position_tracker.open(sample_short_position)
        assert position_tracker.count == 2
        position_tracker.close(sample_long_position.symbol)
        assert position_tracker.count == 1


# ---------------------------------------------------------------------------
# Filtering and aggregation
# ---------------------------------------------------------------------------

class TestPositionTrackerQueries:
    def test_get_all(self, position_tracker, sample_long_position, sample_short_position):
        position_tracker.open(sample_long_position)
        position_tracker.open(sample_short_position)
        all_pos = position_tracker.get_all()
        assert len(all_pos) == 2
        assert sample_long_position in all_pos
        assert sample_short_position in all_pos

    def test_get_by_strategy(self, position_tracker, sample_long_position):
        other = LivePosition(
            symbol="SOL/USDT:USDT",
            side="long",
            qty=Decimal("10"),
            entry_price=Decimal("100"),
            strategy_id="other_strat",
        )
        position_tracker.open(sample_long_position)
        position_tracker.open(other)
        result = position_tracker.get_by_strategy("funding_capture")
        assert len(result) == 1
        assert result[0] is sample_long_position

    def test_get_by_strategy_no_match(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        assert position_tracker.get_by_strategy("nonexistent") == []

    def test_total_exposure(
        self, position_tracker, sample_long_position, sample_short_position
    ):
        position_tracker.open(sample_long_position)
        position_tracker.open(sample_short_position)
        expected = sample_long_position.notional + sample_short_position.notional
        assert position_tracker.total_exposure() == expected

    def test_total_exposure_empty(self, position_tracker):
        assert position_tracker.total_exposure() == Decimal("0")


# ---------------------------------------------------------------------------
# sync_from_exchange
# ---------------------------------------------------------------------------

class TestSyncFromExchange:
    def test_adopts_new_exchange_positions(self, position_tracker):
        exchange_data = [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 0.5,
                "entryPrice": 60000,
                "leverage": 5,
            }
        ]
        position_tracker.sync_from_exchange(exchange_data)
        pos = position_tracker.get("BTC/USDT:USDT")
        assert pos is not None
        assert pos.qty == Decimal("0.5")
        assert pos.entry_price == Decimal("60000")
        assert pos.strategy_id == "unknown"
        assert pos.metadata["synced_from_exchange"] is True

    def test_updates_existing_positions(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        exchange_data = [
            {
                "symbol": sample_long_position.symbol,
                "contracts": 0.005,
                "unrealizedPnl": 42.0,
            }
        ]
        position_tracker.sync_from_exchange(exchange_data)
        pos = position_tracker.get(sample_long_position.symbol)
        assert pos.qty == Decimal("0.005")
        assert pos.unrealized_pnl == Decimal("42.0")
        # strategy_id preserved
        assert pos.strategy_id == "funding_capture"

    def test_removes_stale_positions(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        # Exchange reports no positions at all
        position_tracker.sync_from_exchange([])
        assert position_tracker.count == 0

    def test_skips_zero_contracts(self, position_tracker):
        exchange_data = [
            {"symbol": "XRP/USDT:USDT", "contracts": 0, "entryPrice": 1},
        ]
        position_tracker.sync_from_exchange(exchange_data)
        assert position_tracker.count == 0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestToDict:
    def test_to_dict_returns_list(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        result = position_tracker.to_dict()
        assert isinstance(result, list)
        assert len(result) == 1

    def test_to_dict_fields(self, position_tracker, sample_long_position):
        position_tracker.open(sample_long_position)
        d = position_tracker.to_dict()[0]
        assert d["symbol"] == sample_long_position.symbol
        assert d["side"] == "long"
        assert d["qty"] == str(sample_long_position.qty)
        assert d["entry_price"] == str(sample_long_position.entry_price)
        assert d["leverage"] == sample_long_position.leverage
        assert d["strategy_id"] == "funding_capture"
        assert "opened_at" in d

    def test_to_dict_empty(self, position_tracker):
        assert position_tracker.to_dict() == []
