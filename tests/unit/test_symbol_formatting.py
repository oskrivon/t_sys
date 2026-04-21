"""Tests for symbol format conversions used across the codebase.

Three formats exist:
  - ccxt:  "BTC/USDT:USDT"   (ccxt perpetual futures notation)
  - raw:   "BTCUSDT"          (exchange REST/WS format)
  - base:  "BTC"              (coin name only)

Conversions tested (from funding_capture.py and executor.py):
  ccxt_to_raw:   sym.replace("/", "").replace(":USDT", "")
  raw_to_base:   symbol_raw.replace("USDT", "")
  base_to_ccxt:  f"{base}/USDT:USDT"

CRITICAL BUG DOCUMENTED: raw_to_base uses str.replace("USDT", "")
which removes ALL occurrences of "USDT", not just the trailing suffix.
Any coin whose name contains the substring "USDT" will be mangled.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Conversion functions (extracted from scattered inline usage)
# ---------------------------------------------------------------------------

def ccxt_to_raw(sym: str) -> str:
    """funding_capture.py:115, executor.py:140"""
    return sym.replace("/", "").replace(":USDT", "")


def raw_to_base(symbol_raw: str) -> str:
    """funding_capture.py:183"""
    return symbol_raw.replace("USDT", "")


def base_to_ccxt(base: str) -> str:
    """funding_capture.py:184-185"""
    return f"{base}/USDT:USDT"


# ---------------------------------------------------------------------------
# Round-trip: ccxt -> raw -> base -> ccxt
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """ccxt -> raw -> base -> ccxt should be idempotent for normal symbols."""

    @pytest.mark.parametrize(
        "ccxt_sym, expected_raw, expected_base",
        [
            ("BTC/USDT:USDT", "BTCUSDT", "BTC"),
            ("ETH/USDT:USDT", "ETHUSDT", "ETH"),
            ("SOL/USDT:USDT", "SOLUSDT", "SOL"),
            ("DOGE/USDT:USDT", "DOGEUSDT", "DOGE"),
            ("XRP/USDT:USDT", "XRPUSDT", "XRP"),
        ],
        ids=["BTC", "ETH", "SOL", "DOGE", "XRP"],
    )
    def test_round_trip_normal_symbols(self, ccxt_sym: str, expected_raw: str, expected_base: str):
        raw = ccxt_to_raw(ccxt_sym)
        assert raw == expected_raw

        base = raw_to_base(raw)
        assert base == expected_base

        reconstructed = base_to_ccxt(base)
        assert reconstructed == ccxt_sym

    @pytest.mark.parametrize(
        "ccxt_sym, expected_raw, expected_base",
        [
            ("1000PEPE/USDT:USDT", "1000PEPEUSDT", "1000PEPE"),
            ("1000SHIB/USDT:USDT", "1000SHIBUSDT", "1000SHIB"),
            ("SUSHI/USDT:USDT", "SUSHIUSDT", "SUSHI"),
        ],
        ids=["1000PEPE", "1000SHIB", "SUSHI"],
    )
    def test_round_trip_numeric_prefix_and_special(self, ccxt_sym: str, expected_raw: str, expected_base: str):
        """Symbols with numeric prefixes or unusual names still round-trip."""
        raw = ccxt_to_raw(ccxt_sym)
        assert raw == expected_raw
        base = raw_to_base(raw)
        assert base == expected_base
        assert base_to_ccxt(base) == ccxt_sym


# ---------------------------------------------------------------------------
# ccxt_to_raw edge cases
# ---------------------------------------------------------------------------

class TestCcxtToRaw:
    """Verify ccxt -> raw conversion handles all expected inputs."""

    def test_removes_slash_and_colon_suffix(self):
        assert ccxt_to_raw("BTC/USDT:USDT") == "BTCUSDT"

    def test_idempotent_on_raw(self):
        """Applying ccxt_to_raw on an already-raw symbol is safe."""
        assert ccxt_to_raw("BTCUSDT") == "BTCUSDT"

    def test_spot_symbol_no_colon(self):
        """Spot ccxt format 'BTC/USDT' (no :USDT suffix)."""
        assert ccxt_to_raw("BTC/USDT") == "BTCUSDT"


# ---------------------------------------------------------------------------
# CRITICAL BUG: raw_to_base with "USDT" substring in coin name
# ---------------------------------------------------------------------------

class TestRawToBaseBugs:
    """Document the str.replace('USDT', '') bug.

    The current implementation removes ALL occurrences of 'USDT',
    not just the trailing four characters. This breaks any coin
    whose ticker contains the substring 'USDT'.
    """

    def test_ustc_survives(self):
        """USTC does not contain 'USDT' so it works fine."""
        assert raw_to_base("USTCUSDT") == "USTC"

    def test_usdt_coin_itself_is_broken(self):
        """BUG: 'USDT/USDT:USDT' → raw 'USDTUSDT' → base '' (empty string).

        If someone tried to trade USDT itself (hypothetical), the base
        becomes empty, producing '/USDT:USDT' which is invalid.
        """
        raw = ccxt_to_raw("USDT/USDT:USDT")
        assert raw == "USDTUSDT"
        base = raw_to_base(raw)
        # BUG: both occurrences of USDT are removed → empty string
        assert base == "", "BUG: raw_to_base destroys the symbol entirely"
        # The reconstructed ccxt symbol is broken
        assert base_to_ccxt(base) == "/USDT:USDT", "BUG: invalid ccxt symbol"

    def test_hypothetical_usdtc_coin_broken(self):
        """BUG: A coin 'USDTC' → raw 'USDTCUSDT' → base 'C'.

        replace('USDT', '') strips both the prefix and trailing USDT.
        """
        raw = "USDTCUSDT"
        base = raw_to_base(raw)
        # 'USDTCUSDT'.replace('USDT', '') → first match at pos 0 removes 'USDT' → 'CUSDT'
        #                                   → second match removes 'USDT' → 'C'
        assert base == "C", "BUG: coin name mangled from USDTC to C"

    def test_replace_removes_all_occurrences(self):
        """Demonstrate that str.replace removes ALL matches, not just trailing."""
        # Hypothetical worst case: coin name is literally "USDTUSDT"
        # (base = "USDTUSDT", raw = "USDTUSDTUSDT")
        raw = "USDTUSDTUSDT"
        base = raw_to_base(raw)
        assert base == "", "BUG: triple USDT occurrence yields empty string"


# ---------------------------------------------------------------------------
# Suggested fix: use rstrip or regex for suffix-only removal
# ---------------------------------------------------------------------------

def raw_to_base_fixed(symbol_raw: str) -> str:
    """Correct implementation: only strip trailing USDT suffix."""
    if symbol_raw.endswith("USDT"):
        return symbol_raw[:-4]
    return symbol_raw


class TestFixedRawToBase:
    """Verify the proposed fix handles edge cases correctly."""

    @pytest.mark.parametrize(
        "raw, expected_base",
        [
            ("BTCUSDT", "BTC"),
            ("ETHUSDT", "ETH"),
            ("1000PEPEUSDT", "1000PEPE"),
            ("SUSHIUSDT", "SUSHI"),
            ("USTCUSDT", "USTC"),
            ("USDTCUSDT", "USDTC"),     # Preserves USDT prefix in coin name
            ("USDTUSDT", "USDT"),        # USDT coin itself works
        ],
        ids=["BTC", "ETH", "1000PEPE", "SUSHI", "USTC", "USDTC-fixed", "USDT-coin"],
    )
    def test_fixed_only_strips_trailing_usdt(self, raw: str, expected_base: str):
        assert raw_to_base_fixed(raw) == expected_base
