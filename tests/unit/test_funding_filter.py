"""Tests for FundingCaptureStrategy REST scan filter logic (_do_scan).

Verifies that _do_scan correctly filters tickers by:
- USDT perpetual pair format (/USDT:USDT)
- Minimum 24h volume threshold
- Minimum absolute funding rate (scan_threshold)
- Presence and non-zero fundingRate in ticker info
- ALWAYS_MONITOR symbols are always retained
- Results sorted by rate_bps descending
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.funding_capture import (
    ALWAYS_MONITOR,
    FundingCaptureStrategy,
)
from src.strategies.base import StrategyConfig, StrategyType
from src.engine.event_bus import EventBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticker(
    funding_rate: str | None = "0.0020",
    quote_volume: float = 10_000_000,
    *,
    info_extra: dict | None = None,
) -> dict:
    """Build a minimal ticker dict matching ccxt structure."""
    info = {}
    if funding_rate is not None:
        info["fundingRate"] = funding_rate
    if info_extra:
        info.update(info_extra)
    return {
        "info": info,
        "quoteVolume": quote_volume,
        "last": 100.0,
    }


def _build_tickers(pairs: dict[str, dict]) -> dict:
    """Wrap pairs into a full tickers response."""
    return pairs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def strategy(funding_config, event_bus):
    """FundingCaptureStrategy with default test config."""
    strat = FundingCaptureStrategy(funding_config, event_bus)
    # Set exchange_ref so _do_scan knows which exchange to use
    mock_exchange = MagicMock()
    mock_exchange.id = "bybit"
    strat._exchange_ref = mock_exchange
    return strat


@pytest.fixture
def patch_scanner():
    """Patch ccxt.async_support.bybit inside _do_scan and return the mock
    scanner so tests can set fetch_tickers return value."""
    scanner = AsyncMock()
    scanner.close = AsyncMock()

    mock_bybit_cls = MagicMock(return_value=scanner)

    with patch("ccxt.async_support.bybit", mock_bybit_cls):
        yield scanner


# ---------------------------------------------------------------------------
# Tests — inclusion / exclusion
# ---------------------------------------------------------------------------

class TestScanFilterInclusion:
    """Coins that should pass all filters."""

    async def test_high_rate_high_volume_included(self, strategy, patch_scanner):
        """Coin with rate > scan_threshold and volume > min → included."""
        patch_scanner.fetch_tickers.return_value = {
            "DOGE/USDT:USDT": _make_ticker("0.0020", 10_000_000),
        }

        await strategy._do_scan()

        assert "DOGEUSDT" in strategy._monitored
        assert any(r["symbol"] == "DOGEUSDT" for r in strategy._last_scan_results)

    async def test_negative_rate_included_by_abs(self, strategy, patch_scanner):
        """Negative funding rate should be included (abs used)."""
        patch_scanner.fetch_tickers.return_value = {
            "AVAX/USDT:USDT": _make_ticker("-0.0025", 20_000_000),
        }

        await strategy._do_scan()

        assert "AVAXUSDT" in strategy._monitored
        results = [r for r in strategy._last_scan_results if r["symbol"] == "AVAXUSDT"]
        assert len(results) == 1
        assert results[0]["rate"] == -0.0025  # original sign preserved

    async def test_exact_threshold_rate_included(self, strategy, patch_scanner):
        """Rate exactly at scan_threshold_rate should be included (>= check)."""
        # scan_threshold_bps=10 → scan_threshold_rate=0.001
        patch_scanner.fetch_tickers.return_value = {
            "LINK/USDT:USDT": _make_ticker("0.0010", 8_000_000),
        }

        await strategy._do_scan()

        assert "LINKUSDT" in strategy._monitored


class TestScanFilterExclusion:
    """Coins that should be filtered out."""

    async def test_low_volume_excluded(self, strategy, patch_scanner):
        """Volume below min_volume_24h → excluded even with high rate."""
        patch_scanner.fetch_tickers.return_value = {
            "SHIB/USDT:USDT": _make_ticker("0.0050", 1_000_000),  # < 5M
        }

        await strategy._do_scan()

        # SHIB is not in ALWAYS_MONITOR, should not appear in scan results
        assert not any(r["symbol"] == "SHIBUSDT" for r in strategy._last_scan_results)

    async def test_low_rate_excluded(self, strategy, patch_scanner):
        """Rate below scan_threshold → excluded even with high volume."""
        patch_scanner.fetch_tickers.return_value = {
            "ADA/USDT:USDT": _make_ticker("0.0005", 50_000_000),  # < 0.001
        }

        await strategy._do_scan()

        assert not any(r["symbol"] == "ADAUSDT" for r in strategy._last_scan_results)

    async def test_non_usdt_pair_excluded(self, strategy, patch_scanner):
        """Non-USDT perpetual pair → skipped entirely."""
        patch_scanner.fetch_tickers.return_value = {
            "BTC/BTC:BTC": _make_ticker("0.0100", 100_000_000),
            "ETH/USD:ETH": _make_ticker("0.0100", 100_000_000),
        }

        await strategy._do_scan()

        # Only ALWAYS_MONITOR symbols should be present, no scan results
        assert strategy._last_scan_results == []

    async def test_missing_funding_rate_excluded(self, strategy, patch_scanner):
        """Ticker with no fundingRate in info → skipped."""
        patch_scanner.fetch_tickers.return_value = {
            "MATIC/USDT:USDT": {"info": {}, "quoteVolume": 20_000_000, "last": 1.0},
        }

        await strategy._do_scan()

        assert not any(r["symbol"] == "MATICUSDT" for r in strategy._last_scan_results)

    async def test_zero_funding_rate_excluded(self, strategy, patch_scanner):
        """fundingRate = '0' is falsy in Python → skipped by `if not fr`."""
        patch_scanner.fetch_tickers.return_value = {
            "UNI/USDT:USDT": _make_ticker("0", 15_000_000),
        }

        await strategy._do_scan()

        assert not any(r["symbol"] == "UNIUSDT" for r in strategy._last_scan_results)

    async def test_none_quote_volume_treated_as_zero(self, strategy, patch_scanner):
        """quoteVolume=None → float(0) → below min volume."""
        patch_scanner.fetch_tickers.return_value = {
            "FIL/USDT:USDT": {
                "info": {"fundingRate": "0.005"},
                "quoteVolume": None,
                "last": 5.0,
            },
        }

        await strategy._do_scan()

        assert not any(r["symbol"] == "FILUSDT" for r in strategy._last_scan_results)

    async def test_volume_exactly_at_threshold_excluded(self, strategy, patch_scanner):
        """Volume exactly at min_volume_24h should still be excluded (strict <)."""
        # min_volume_24h = 5_000_000, filter is `vol_24h < self._min_volume_24h`
        # So exactly 5M passes the filter (is NOT less than 5M)
        patch_scanner.fetch_tickers.return_value = {
            "APE/USDT:USDT": _make_ticker("0.0020", 5_000_000),
        }

        await strategy._do_scan()

        # Exactly at threshold: 5M is NOT < 5M, so it passes
        assert any(r["symbol"] == "APEUSDT" for r in strategy._last_scan_results)


class TestAlwaysMonitor:
    """ALWAYS_MONITOR symbols should remain in _monitored regardless of scan."""

    async def test_always_monitor_present_with_empty_tickers(self, strategy, patch_scanner):
        """Even with zero tickers, ALWAYS_MONITOR stays in _monitored."""
        patch_scanner.fetch_tickers.return_value = {}

        await strategy._do_scan()

        for sym in ALWAYS_MONITOR:
            assert sym in strategy._monitored

    async def test_always_monitor_not_removed_on_rescan(self, strategy, patch_scanner):
        """ALWAYS_MONITOR symbols are never removed from _monitored."""
        # First scan: add some extra symbols
        patch_scanner.fetch_tickers.return_value = {
            "AVAX/USDT:USDT": _make_ticker("0.005", 20_000_000),
        }
        await strategy._do_scan()
        assert "AVAXUSDT" in strategy._monitored

        # Second scan: no hot symbols from tickers
        patch_scanner.fetch_tickers.return_value = {}
        await strategy._do_scan()

        # AVAX removed, but ALWAYS_MONITOR symbols remain
        assert "AVAXUSDT" not in strategy._monitored
        for sym in ALWAYS_MONITOR:
            assert sym in strategy._monitored


class TestScanResultsSorting:
    """Scan results should be sorted by rate_bps descending."""

    async def test_results_sorted_by_rate_bps_descending(self, strategy, patch_scanner):
        """Multiple coins sorted from highest to lowest abs rate."""
        patch_scanner.fetch_tickers.return_value = {
            "LINK/USDT:USDT": _make_ticker("0.0010", 10_000_000),   # 10 bps
            "AVAX/USDT:USDT": _make_ticker("-0.0050", 10_000_000),  # 50 bps
            "MATIC/USDT:USDT": _make_ticker("0.0030", 10_000_000),  # 30 bps
        }

        await strategy._do_scan()

        results = strategy._last_scan_results
        assert len(results) == 3
        assert results[0]["symbol"] == "AVAXUSDT"
        assert results[1]["symbol"] == "MATICUSDT"
        assert results[2]["symbol"] == "LINKUSDT"

        # Verify rate_bps values
        bps_values = [r["rate_bps"] for r in results]
        assert bps_values == sorted(bps_values, reverse=True)

    async def test_scan_results_contain_correct_fields(self, strategy, patch_scanner):
        """Each scan result has symbol, rate, rate_bps, volume_24h."""
        patch_scanner.fetch_tickers.return_value = {
            "ARB/USDT:USDT": _make_ticker("0.0020", 12_000_000),
        }

        await strategy._do_scan()

        result = strategy._last_scan_results[0]
        assert result["symbol"] == "ARBUSDT"
        assert result["rate"] == pytest.approx(0.002)
        assert result["rate_bps"] == pytest.approx(20.0)
        assert result["volume_24h"] == pytest.approx(12_000_000)


class TestSymbolNormalization:
    """Verify ccxt symbol → raw symbol conversion."""

    async def test_symbol_slash_and_colon_removed(self, strategy, patch_scanner):
        """'FOO/USDT:USDT' becomes 'FOOUSDT'."""
        patch_scanner.fetch_tickers.return_value = {
            "1000PEPE/USDT:USDT": _make_ticker("0.0100", 50_000_000),
        }

        await strategy._do_scan()

        assert "1000PEPEUSDT" in strategy._monitored
        assert strategy._last_scan_results[0]["symbol"] == "1000PEPEUSDT"
