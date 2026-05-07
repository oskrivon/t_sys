"""Tests for FundingCaptureStrategy REST scanner on Binance.

Binance doesn't embed fundingRate in tickers — it uses a separate
fetch_funding_rates() call. Verifies the scanner handles this correctly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.funding_capture import FundingCaptureStrategy
from src.strategies.base import StrategyConfig, StrategyType
from src.engine.event_bus import EventBus


def _make_config(**overrides) -> StrategyConfig:
    params = {
        "threshold_bps": 15.0,
        "scan_threshold_bps": 10.0,
        "leverage": 10,
        "min_volume_24h": 5_000_000,
        "target_notional": 25.0,
    }
    params.update(overrides)
    return StrategyConfig(
        strategy_id="funding_capture", name="FC",
        strategy_type=StrategyType.EVENT_DRIVEN,
        allocation_pct=10.0, max_positions=20, params=params,
    )


@pytest.fixture
def strategy():
    strat = FundingCaptureStrategy(_make_config(), EventBus())
    # Binance exchange ref
    mock_exchange = MagicMock()
    mock_exchange.id = "binance"
    strat._exchange_ref = mock_exchange
    return strat


@pytest.fixture
def patch_binance_scanner():
    """Patch ccxt.async_support.binance for _do_scan."""
    scanner = AsyncMock()
    scanner.close = AsyncMock()

    # Tickers (volume data only, no funding)
    scanner.fetch_tickers.return_value = {
        "BTC/USDT:USDT": {
            "info": {"symbol": "BTCUSDT"},
            "quoteVolume": 500_000_000,
            "last": 67000.0,
        },
        "CHIP/USDT:USDT": {
            "info": {"symbol": "CHIPUSDT"},
            "quoteVolume": 10_000_000,
            "last": 0.05,
        },
        "LOWVOL/USDT:USDT": {
            "info": {"symbol": "LOWVOLUSDT"},
            "quoteVolume": 1_000_000,  # below min
            "last": 1.0,
        },
    }

    # Funding rates (separate call on Binance)
    scanner.fetch_funding_rates.return_value = {
        "BTC/USDT:USDT": {
            "fundingRate": 0.0001,  # 1 bps — below threshold
            "fundingTimestamp": 1700000000000,
            "info": {"nextFundingTime": "1700000000000"},
        },
        "CHIP/USDT:USDT": {
            "fundingRate": -0.0025,  # 25 bps — above threshold
            "fundingTimestamp": 1700000000000,
            "info": {"nextFundingTime": "1700000000000"},
        },
        "LOWVOL/USDT:USDT": {
            "fundingRate": 0.005,  # 50 bps but low volume
            "fundingTimestamp": 1700000000000,
            "info": {"nextFundingTime": "1700000000000"},
        },
    }

    mock_cls = MagicMock(return_value=scanner)
    with patch("ccxt.async_support.binance", mock_cls):
        yield scanner


class TestBinanceScanBasic:
    async def test_high_rate_high_volume_included(self, strategy, patch_binance_scanner):
        await strategy._do_scan()

        assert "CHIPUSDT" in strategy._monitored
        results = [r for r in strategy._last_scan_results if r["symbol"] == "CHIPUSDT"]
        assert len(results) == 1
        assert results[0]["rate"] == -0.0025
        assert results[0]["rate_bps"] == pytest.approx(25.0)

    async def test_low_rate_excluded(self, strategy, patch_binance_scanner):
        await strategy._do_scan()

        # BTC has only 1 bps funding — below 10 bps threshold
        assert not any(r["symbol"] == "BTCUSDT" for r in strategy._last_scan_results)

    async def test_low_volume_excluded(self, strategy, patch_binance_scanner):
        await strategy._do_scan()

        assert not any(r["symbol"] == "LOWVOLUSDT" for r in strategy._last_scan_results)

    async def test_no_category_param_for_binance(self, strategy, patch_binance_scanner):
        """Binance should NOT get category=linear param (that's Bybit-specific)."""
        await strategy._do_scan()

        call_kwargs = patch_binance_scanner.fetch_tickers.call_args
        params = call_kwargs[1].get("params", {}) if call_kwargs[1] else call_kwargs[0][0] if call_kwargs[0] else {}
        assert "category" not in params

    async def test_fetch_funding_rates_called(self, strategy, patch_binance_scanner):
        """Binance path should call fetch_funding_rates() separately."""
        await strategy._do_scan()

        patch_binance_scanner.fetch_funding_rates.assert_called_once()

    async def test_scanner_closed(self, strategy, patch_binance_scanner):
        await strategy._do_scan()

        patch_binance_scanner.close.assert_called_once()


class TestBinanceScanEdgeCases:
    async def test_symbol_not_in_funding_rates_excluded(self, strategy, patch_binance_scanner):
        """If a ticker exists but has no funding rate data, skip it."""
        patch_binance_scanner.fetch_tickers.return_value["NEW/USDT:USDT"] = {
            "info": {}, "quoteVolume": 50_000_000, "last": 1.0,
        }
        # No entry in fetch_funding_rates for NEW/USDT:USDT

        await strategy._do_scan()

        assert not any(r["symbol"] == "NEWUSDT" for r in strategy._last_scan_results)

    async def test_zero_funding_rate_excluded(self, strategy, patch_binance_scanner):
        patch_binance_scanner.fetch_funding_rates.return_value["BTC/USDT:USDT"]["fundingRate"] = 0.0

        await strategy._do_scan()

        assert not any(r["symbol"] == "BTCUSDT" for r in strategy._last_scan_results)

    async def test_none_funding_rate_excluded(self, strategy, patch_binance_scanner):
        patch_binance_scanner.fetch_funding_rates.return_value["BTC/USDT:USDT"]["fundingRate"] = None

        await strategy._do_scan()

        assert not any(r["symbol"] == "BTCUSDT" for r in strategy._last_scan_results)
