"""Unit tests for the Phase-0 structure tagger + bar builder."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "icebreaker_structure_backtest",
    Path(__file__).resolve().parents[2] / "scripts" / "research"
    / "icebreaker_structure_backtest.py")
stb = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stb
_spec.loader.exec_module(stb)


def test_bars_from_trades_ohlcv():
    # two 1s bars (bar_ms=1000): bucket 0 -> [10,12,9,11], bucket 1 -> [20]
    rows = [(100, 10.0, 1.0), (400, 12.0, 2.0), (600, 9.0, 1.0),
            (900, 11.0, 1.0), (1500, 20.0, 3.0)]
    bars = stb.bars_from_trades(rows, 1000)
    assert [b["ts"] for b in bars] == [0, 1000]
    b0 = bars[0]
    assert (b0["open"], b0["high"], b0["low"], b0["close"]) == (10.0, 12.0, 9.0, 11.0)
    assert b0["volume"] == 5.0
    assert bars[1]["open"] == bars[1]["close"] == 20.0


def _flat_bars(price, n, bar_ms=1000, width=0.0):
    """n bars hugging `price` with optional +/- width band."""
    out = []
    for i in range(n):
        hi = price * (1 + width)
        lo = price * (1 - width)
        out.append({"ts": i * bar_ms, "open": price, "high": hi,
                    "low": lo, "close": price, "volume": 1.0})
    return out


def test_resistance_breakout_is_tagged():
    # 20 bars consolidating just under 100; ask wall at 100 (resistance).
    bars = _flat_bars(99.7, 20, width=0.001)
    # make highs repeatedly tag 100 (touches)
    for b in bars:
        b["high"] = 100.0
    sig_ts = 20 * 1000  # just after the consolidation
    bars.append({"ts": sig_ts, "open": 100.0, "high": 101.0, "low": 100.0,
                 "close": 101.0, "volume": 5.0})
    tags = stb.tag_structure(bars, sig_ts, 100.0, "ask",
                             lookback_bars=48, consol_bars=12, tol=0.0015,
                             range_thresh=0.02, min_touches=2,
                             squeeze_ratio=0.7, edge_tol=0.003)
    assert tags["at_level"] is True
    assert tags["consolidation"] is True
    assert tags["at_edge"] is True   # wall == top of the range


def test_random_wall_not_at_level():
    # price nowhere near the wall, wide & trending -> no structure
    bars = [{"ts": i * 1000, "open": 50 + i, "high": 51 + i, "low": 49 + i,
             "close": 50 + i, "volume": 1.0} for i in range(30)]
    sig_ts = 30 * 1000
    tags = stb.tag_structure(bars, sig_ts, 100.0, "ask",
                             lookback_bars=48, consol_bars=12, tol=0.0015,
                             range_thresh=0.02, min_touches=2,
                             squeeze_ratio=0.7, edge_tol=0.003)
    assert tags["at_level"] is False
    assert tags["at_edge"] is False


def test_geo_gated_requires_all_three():
    assert stb.geo_gated({"at_level": True, "consolidation": True, "at_edge": True})
    assert not stb.geo_gated({"at_level": True, "consolidation": True, "at_edge": False})
    assert not stb.geo_gated({"at_level": False, "consolidation": True, "at_edge": True})


def test_squeeze_detects_contraction():
    # earlier half wide, recent half tight -> squeeze
    bars = []
    for i in range(6):       # wide
        bars.append({"ts": i * 1000, "open": 100, "high": 104, "low": 96,
                     "close": 100, "volume": 1.0})
    for i in range(6, 12):   # tight
        bars.append({"ts": i * 1000, "open": 100, "high": 100.5, "low": 99.5,
                     "close": 100, "volume": 1.0})
    sig_ts = 12 * 1000
    bars.append({"ts": sig_ts, "open": 100, "high": 101, "low": 100,
                 "close": 101, "volume": 1.0})
    tags = stb.tag_structure(bars, sig_ts, 100.0, "ask",
                             lookback_bars=48, consol_bars=12, tol=0.05,
                             range_thresh=0.2, min_touches=1,
                             squeeze_ratio=0.7, edge_tol=0.05)
    assert tags["squeeze"] is True
