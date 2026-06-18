"""Unit tests for icebreaker cross-exchange universe selection (Этап 0)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.models.base import Exchange
from src.icebreaker.universe import (
    DEFAULT_EXCLUDE_BASES,
    PerpInfo,
    fetch_perp_universe,
    perp_infos_from_markets,
    select_overlap_universe,
)


def _perp(exchange: str, base: str, vol: float, active: bool = True) -> PerpInfo:
    return PerpInfo(
        exchange=exchange,
        base=base,
        symbol=f"{base}/USDT:USDT",
        quote_volume_usd=vol,
        active=active,
    )


def _universe(**by_exchange: dict[str, PerpInfo]):
    return dict(by_exchange)


# ---------------------------------------------------------------------------
# select_overlap_universe — core selection logic
# ---------------------------------------------------------------------------

def test_basic_overlap_included():
    """A mid-illiquid base on bybit + mexc is selected."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 5_000_000)},
        mexc={"FOO": _perp("mexc", "FOO", 2_000_000)},
    )
    out = select_overlap_universe(perps)
    assert [c.base for c in out] == ["FOO"]
    assert out[0].execution_symbol == "FOO/USDT:USDT"
    assert out[0].detect_venues == {"mexc": "FOO/USDT:USDT"}
    assert out[0].detect_volume_usd == {"mexc": 2_000_000}


def test_requires_execution_listing():
    """Base only on the detect venue (not bybit) cannot be entered → dropped."""
    perps = _universe(
        bybit={},
        mexc={"FOO": _perp("mexc", "FOO", 3_000_000)},
    )
    assert select_overlap_universe(perps) == []


def test_requires_a_detect_venue():
    """Base only on bybit (no second book to watch) → dropped."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 3_000_000)},
        mexc={},
    )
    assert select_overlap_universe(perps) == []


def test_excludes_big_caps():
    """Bases in the exclusion list are dropped even if in-band on both venues."""
    perps = _universe(
        bybit={"BTC": _perp("bybit", "BTC", 5_000_000),
               "FOO": _perp("bybit", "FOO", 5_000_000)},
        mexc={"BTC": _perp("mexc", "BTC", 5_000_000),
              "FOO": _perp("mexc", "FOO", 5_000_000)},
    )
    out = select_overlap_universe(perps)
    assert [c.base for c in out] == ["FOO"]


def test_excludes_stable_bases():
    """Stable/pegged bases (USDC etc.) are always excluded."""
    perps = _universe(
        bybit={"USDC": _perp("bybit", "USDC", 5_000_000)},
        mexc={"USDC": _perp("mexc", "USDC", 5_000_000)},
    )
    assert select_overlap_universe(perps) == []


def test_volume_band_upper_bound():
    """Too-liquid (a major by volume) is excluded by max_volume_usd."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 80_000_000)},
        mexc={"FOO": _perp("mexc", "FOO", 80_000_000)},
    )
    assert select_overlap_universe(perps, max_volume_usd=50_000_000) == []


def test_volume_band_lower_bound():
    """Too-illiquid (un-fillable on bybit) is excluded by min_volume_usd."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 100_000)},
        mexc={"FOO": _perp("mexc", "FOO", 100_000)},
    )
    assert select_overlap_universe(perps, min_volume_usd=1_000_000) == []


def test_band_uses_execution_volume_only():
    """Liquidity band is judged on the execution venue, not the detect venue."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 5_000_000)},
        # detect venue is far below the floor — must NOT disqualify
        mexc={"FOO": _perp("mexc", "FOO", 1_000)},
    )
    out = select_overlap_universe(perps, min_volume_usd=1_000_000)
    assert [c.base for c in out] == ["FOO"]


def test_inactive_execution_dropped():
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 5_000_000, active=False)},
        mexc={"FOO": _perp("mexc", "FOO", 5_000_000)},
    )
    assert select_overlap_universe(perps) == []


def test_inactive_detect_not_counted():
    """An inactive detect listing doesn't count as a venue → dropped if it's the only one."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 5_000_000)},
        mexc={"FOO": _perp("mexc", "FOO", 5_000_000, active=False)},
    )
    assert select_overlap_universe(perps) == []


def test_sorting_by_detect_count_then_illiquidity():
    """More detect venues first; ties broken by most-illiquid (lowest exec vol)."""
    perps = _universe(
        bybit={
            "AAA": _perp("bybit", "AAA", 9_000_000),   # 1 venue
            "BBB": _perp("bybit", "BBB", 8_000_000),   # 2 venues
            "CCC": _perp("bybit", "CCC", 4_000_000),   # 2 venues, more illiquid
        },
        mexc={"AAA": _perp("mexc", "AAA", 1), "BBB": _perp("mexc", "BBB", 1),
              "CCC": _perp("mexc", "CCC", 1)},
        kucoinfutures={"BBB": _perp("kucoinfutures", "BBB", 1),
                       "CCC": _perp("kucoinfutures", "CCC", 1)},
    )
    out = select_overlap_universe(perps)
    assert [c.base for c in out] == ["CCC", "BBB", "AAA"]


def test_limit_caps_results():
    perps = _universe(
        bybit={b: _perp("bybit", b, 1_000_000 + i)
               for i, b in enumerate(["AAA", "BBB", "CCC"])},
        mexc={b: _perp("mexc", b, 1) for b in ["AAA", "BBB", "CCC"]},
    )
    out = select_overlap_universe(perps, limit=2)
    assert len(out) == 2


def test_explicit_detect_exchanges_filter():
    """Only the named detect exchanges are considered."""
    perps = _universe(
        bybit={"FOO": _perp("bybit", "FOO", 5_000_000)},
        mexc={"FOO": _perp("mexc", "FOO", 2_000_000)},
        kucoinfutures={"FOO": _perp("kucoinfutures", "FOO", 3_000_000)},
    )
    out = select_overlap_universe(perps, detect_exchanges=["mexc"])
    assert out[0].detect_venues == {"mexc": "FOO/USDT:USDT"}


def test_missing_execution_exchange_returns_empty():
    perps = _universe(mexc={"FOO": _perp("mexc", "FOO", 2_000_000)})
    assert select_overlap_universe(perps, execution_exchange="bybit") == []


def test_default_exclude_list_matches_backtest():
    """Guard the exclusion list against accidental edits."""
    for b in ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK", "LTC", "DOGE", "HYPE", "TRUMP"):
        assert b in DEFAULT_EXCLUDE_BASES


# ---------------------------------------------------------------------------
# perp_infos_from_markets — ccxt market dict → PerpInfo
# ---------------------------------------------------------------------------

def _mkt(symbol, base, **over):
    m = {"symbol": symbol, "base": base, "quote": "USDT", "settle": "USDT",
         "swap": True, "linear": True, "active": True}
    m.update(over)
    return m


def test_markets_keep_only_usdt_linear_swaps():
    markets = [
        _mkt("FOO/USDT:USDT", "FOO"),
        _mkt("BAR/USD:BAR", "BAR", swap=True, linear=False, settle="BAR"),  # inverse
        _mkt("BAZ/USDT", "BAZ", swap=False),                                  # spot
        _mkt("QUX/USDC:USDC", "QUX", quote="USDC", settle="USDC"),           # non-USDT
        _mkt("OFF/USDT:USDT", "OFF", active=False),                           # inactive
    ]
    out = perp_infos_from_markets("bybit", markets)
    assert set(out) == {"FOO"}


def test_markets_volume_mapping_and_dedup():
    markets = [
        _mkt("FOO/USDT:USDT", "FOO"),
        _mkt("FOO1/USDT:USDT", "FOO"),  # duplicate base, different listing
    ]
    vols = {"FOO/USDT:USDT": 1_000_000, "FOO1/USDT:USDT": 9_000_000}
    out = perp_infos_from_markets("bybit", markets, vols)
    # Higher-volume listing wins the base slot.
    assert out["FOO"].symbol == "FOO1/USDT:USDT"
    assert out["FOO"].quote_volume_usd == 9_000_000


def test_markets_missing_volume_defaults_zero():
    out = perp_infos_from_markets("bybit", [_mkt("FOO/USDT:USDT", "FOO")])
    assert out["FOO"].quote_volume_usd == 0.0


def test_end_to_end_from_markets():
    """Markets → PerpInfo → selection, exercising the full pure pipeline."""
    bybit_markets = [_mkt("FOO/USDT:USDT", "FOO"), _mkt("BTC/USDT:USDT", "BTC")]
    mexc_markets = [_mkt("FOO/USDT:USDT", "FOO")]
    perps = {
        "bybit": perp_infos_from_markets(
            "bybit", bybit_markets,
            {"FOO/USDT:USDT": 5_000_000, "BTC/USDT:USDT": 5_000_000}),
        "mexc": perp_infos_from_markets(
            "mexc", mexc_markets, {"FOO/USDT:USDT": 2_000_000}),
    }
    out = select_overlap_universe(perps)
    assert [c.base for c in out] == ["FOO"]


# ---------------------------------------------------------------------------
# fetch_perp_universe — network wiring (mocked ccxt client, no network)
# ---------------------------------------------------------------------------

class _FakeAdapter:
    """Minimal stand-in for a connected CCXTAdapter."""
    def __init__(self, exchange: Exchange, markets: dict, tickers: dict):
        self.exchange = exchange
        self.client = SimpleNamespace(
            markets=markets,
            fetch_tickers=AsyncMock(return_value=tickers),
        )


async def test_fetch_perp_universe_wires_markets_and_volume():
    markets = {
        "FOO/USDT:USDT": _mkt("FOO/USDT:USDT", "FOO"),
        "BTC/USDT": _mkt("BTC/USDT", "BTC", swap=False),  # spot, ignored
    }
    tickers = {"FOO/USDT:USDT": {"quoteVolume": 4_200_000.0}}
    adapter = _FakeAdapter(Exchange.BYBIT, markets, tickers)

    out = await fetch_perp_universe(adapter)
    assert set(out) == {"FOO"}
    assert out["FOO"].exchange == "bybit"
    assert out["FOO"].quote_volume_usd == 4_200_000.0


async def test_fetch_perp_universe_volume_fallback_to_info():
    markets = {"FOO/USDT:USDT": _mkt("FOO/USDT:USDT", "FOO")}
    # No top-level quoteVolume — fall back to a raw venue field.
    tickers = {"FOO/USDT:USDT": {"info": {"turnover24h": "1234567"}}}
    adapter = _FakeAdapter(Exchange.MEXC, markets, tickers)

    out = await fetch_perp_universe(adapter)
    assert out["FOO"].quote_volume_usd == pytest.approx(1_234_567.0)


async def test_fetch_perp_universe_survives_ticker_failure():
    markets = {"FOO/USDT:USDT": _mkt("FOO/USDT:USDT", "FOO")}
    adapter = _FakeAdapter(Exchange.KUCOIN, markets, {})
    adapter.client.fetch_tickers = AsyncMock(side_effect=RuntimeError("rate limit"))

    out = await fetch_perp_universe(adapter)
    # Still builds the universe; volume just degrades to 0.0.
    assert out["FOO"].quote_volume_usd == 0.0
