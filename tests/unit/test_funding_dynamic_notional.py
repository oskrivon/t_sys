"""Tests for dynamic notional sizing based on account balance.

FundingCaptureStrategy can compute notional from live balance instead of
a fixed target_notional.  When target_notional=0, precompute fetches
free USDT balance and uses balance * balance_fraction (capped by
max_notional).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.funding_capture import FundingCaptureStrategy, FundingOpportunity, Side
from src.strategies.base import StrategyConfig, StrategyType
from src.engine.event_bus import EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> StrategyConfig:
    params = {
        "threshold_bps": 15.0,
        "scan_threshold_bps": 10.0,
        "leverage": 10,
        "entry_seconds_before": 5,
        "exit_seconds_after": 30,
        "min_volume_24h": 5_000_000,
        "target_notional": 0.0,       # dynamic by default
        "max_notional": 500.0,
        "balance_fraction": 0.9,
        "max_spread_bps": 5.0,
    }
    params.update(overrides)
    return StrategyConfig(
        strategy_id="funding_capture",
        name="Funding Capture",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=10.0,
        max_positions=20,
        params=params,
    )


def _make_opp(price: float = 1.0) -> FundingOpportunity:
    return FundingOpportunity(
        symbol_raw="TESTUSDT",
        symbol_ccxt="TEST/USDT:USDT",
        funding_rate=0.002,       # 20 bps
        next_funding_time=9999999999999,
        direction=Side.SHORT,
        last_price=price,
    )


def _mock_exchange(free_usdt: float = 100.0, market_info: dict | None = None) -> MagicMock:
    """Build a mock raw ccxt exchange with fetch_balance and market().

    market() is synchronous in ccxt, so we use MagicMock as base and
    attach async methods explicitly.
    """
    exchange = MagicMock()
    # Sync methods
    exchange.market.return_value = market_info or {
        "limits": {"amount": {"min": 1.0}},
        "precision": {"amount": 1.0},
    }
    # Async methods
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": {"free": free_usdt, "used": 0, "total": free_usdt},
    })
    exchange.set_leverage = AsyncMock()
    exchange.fetch_order_book = AsyncMock(return_value={
        "bids": [[1.0, 500.0], [0.999, 500.0], [0.998, 500.0], [0.997, 500.0], [0.996, 500.0]],
        "asks": [[1.001, 500.0], [1.002, 500.0], [1.003, 500.0], [1.004, 500.0], [1.005, 500.0]],
    })
    return exchange


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestDynamicNotionalConfig:
    """Verify that new config params are parsed correctly."""

    def test_target_notional_zero_enables_dynamic(self):
        strat = FundingCaptureStrategy(_make_config(target_notional=0), EventBus())
        assert strat._target_notional == 0.0

    def test_max_notional_parsed(self):
        strat = FundingCaptureStrategy(_make_config(max_notional=200.0), EventBus())
        assert strat._max_notional == 200.0

    def test_balance_fraction_parsed(self):
        strat = FundingCaptureStrategy(_make_config(balance_fraction=0.8), EventBus())
        assert strat._balance_fraction == 0.8

    def test_fixed_notional_still_works(self):
        strat = FundingCaptureStrategy(_make_config(target_notional=50.0), EventBus())
        assert strat._target_notional == 50.0

    def test_defaults_when_not_specified(self):
        """Config without explicit values should use code defaults."""
        cfg = StrategyConfig(
            strategy_id="fc", name="FC",
            strategy_type=StrategyType.EVENT_DRIVEN,
            allocation_pct=10.0, max_positions=20, params={},
        )
        strat = FundingCaptureStrategy(cfg, EventBus())
        assert strat._target_notional == 0.0
        assert strat._max_notional == 500.0
        assert strat._balance_fraction == 0.9


# ---------------------------------------------------------------------------
# Precompute — dynamic notional
# ---------------------------------------------------------------------------

class TestPrecomputeDynamicNotional:
    """_precompute_entry should use balance when target_notional=0."""

    async def test_uses_balance_when_target_zero(self):
        strat = FundingCaptureStrategy(_make_config(target_notional=0), EventBus())
        strat._exchange_ref = _mock_exchange(free_usdt=50.0)

        result = await strat._precompute_entry(_make_opp(price=1.0))

        assert result is not None
        # 50 * 0.9 = 45 → qty = 45 (at price 1.0)
        assert result["notional_target"] == pytest.approx(45.0)
        assert result["qty"] == pytest.approx(45.0)

    async def test_caps_at_max_notional(self):
        strat = FundingCaptureStrategy(
            _make_config(target_notional=0, max_notional=30.0), EventBus()
        )
        strat._exchange_ref = _mock_exchange(free_usdt=100.0)

        result = await strat._precompute_entry(_make_opp(price=1.0))

        # 100 * 0.9 = 90, but capped at max_notional=30
        assert result["notional_target"] == pytest.approx(30.0)

    async def test_respects_balance_fraction(self):
        strat = FundingCaptureStrategy(
            _make_config(target_notional=0, balance_fraction=0.5), EventBus()
        )
        strat._exchange_ref = _mock_exchange(free_usdt=80.0)

        result = await strat._precompute_entry(_make_opp(price=1.0))

        # 80 * 0.5 = 40
        assert result["notional_target"] == pytest.approx(40.0)

    async def test_fixed_notional_ignores_balance(self):
        strat = FundingCaptureStrategy(_make_config(target_notional=25.0), EventBus())
        strat._exchange_ref = _mock_exchange(free_usdt=1000.0)

        result = await strat._precompute_entry(_make_opp(price=1.0))

        assert result is not None
        # Should use fixed 25, not 1000*0.9=900
        assert result["qty"] == pytest.approx(25.0)

    async def test_balance_fetch_failure_falls_back(self):
        strat = FundingCaptureStrategy(_make_config(target_notional=0), EventBus())
        exchange = _mock_exchange()
        exchange.fetch_balance.side_effect = Exception("network error")
        strat._exchange_ref = exchange

        result = await strat._precompute_entry(_make_opp(price=1.0))

        assert result is not None
        # Fallback to $25
        assert result["notional_target"] == pytest.approx(25.0)
        assert result["qty"] == pytest.approx(25.0)

    async def test_zero_balance_uses_min_qty(self):
        strat = FundingCaptureStrategy(_make_config(target_notional=0), EventBus())
        strat._exchange_ref = _mock_exchange(free_usdt=0.0)

        result = await strat._precompute_entry(_make_opp(price=1.0))

        assert result is not None
        # 0 * 0.9 = 0 → notional_target=0 → qty=min_qty=1
        assert result["qty"] >= 1.0

    async def test_notional_target_in_result(self):
        """precompute result must include notional_target for metadata."""
        strat = FundingCaptureStrategy(_make_config(target_notional=0), EventBus())
        strat._exchange_ref = _mock_exchange(free_usdt=60.0)

        result = await strat._precompute_entry(_make_opp(price=1.0))

        assert "notional_target" in result
        assert result["notional_target"] == pytest.approx(54.0)  # 60*0.9

    async def test_qty_respects_precision(self):
        """qty should be rounded to market's qty_step."""
        strat = FundingCaptureStrategy(_make_config(target_notional=0), EventBus())
        exchange = _mock_exchange(free_usdt=50.0)
        exchange.market.return_value = {
            "limits": {"amount": {"min": 0.01}},
            "precision": {"amount": 0.01},
        }
        strat._exchange_ref = exchange

        result = await strat._precompute_entry(_make_opp(price=0.33))

        # 50*0.9=45 / 0.33 = 136.36... → rounded to 0.01 step = 136.36
        assert result["qty"] == pytest.approx(int(45.0 / 0.33 / 0.01) * 0.01)
