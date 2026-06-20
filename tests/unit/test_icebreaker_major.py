"""Unit tests for major-level swing clustering + breakout detection."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mj = _load("icebreaker_major", "scripts/research/icebreaker_major.py")


def test_cluster_levels_groups_within_tol():
    pts = [(100.0, 0), (100.1, 5), (100.05, 9), (110.0, 12)]
    cl = mj.cluster_levels(pts, tol=0.003)
    levels = sorted(round(l, 1) for l, _ in cl)
    assert levels == [100.0, 110.0]            # 100.x grouped, 110 separate
    big = [m for l, m in cl if l < 105][0]
    assert len(big) == 3


def test_find_swings_picks_local_extrema():
    bars = [{"ts": i, "open": 0, "close": 0,
             "high": h, "low": h - 1}
            for i, h in enumerate([1, 2, 5, 2, 1, 2, 6, 2, 1])]
    sh, sl = mj.find_swings(bars, w=2)
    assert 2 in sh and 6 in sh        # peaks at idx 2 and 6


def _level_touched(level, n_touches, span, bar_ms=1000):
    """Bars that poke `level` from below n_touches times over `span`, flat base."""
    bars = []
    gap = span // n_touches
    for i in range(span):
        touch = (i % gap == 0) and (i // gap) < n_touches
        h = level if touch else level * 0.997
        bars.append({"ts": i * bar_ms, "open": level * 0.996,
                     "high": h, "low": level * 0.994, "close": level * 0.997})
    return bars


def test_detect_major_long_breakout():
    bars = _level_touched(100.0, n_touches=5, span=60)
    # append a decisive breakout bar above the level
    bars.append({"ts": 60_000, "open": 99.8, "high": 101.0, "low": 99.8,
                 "close": 100.8, "volume": 1.0})
    out = mj.detect_major_breakouts(
        bars, 1000, lookback=60, swing_w=2, tol=0.003, min_touches=4,
        min_span_bars=20, brk=0.0015, cooldown=5, near_bars=10, near_tol=0.01)
    assert len(out) >= 1
    o = out[0]
    assert o["side"] == "long"
    assert abs(o["level"] - 100.0) / 100.0 < 0.003
    assert o["touches"] >= 4


def test_no_breakout_without_enough_touches():
    bars = _level_touched(100.0, n_touches=2, span=60)   # only 2 touches
    bars.append({"ts": 60_000, "open": 99.8, "high": 101.0, "low": 99.8,
                 "close": 100.8, "volume": 1.0})
    out = mj.detect_major_breakouts(
        bars, 1000, lookback=60, swing_w=2, tol=0.003, min_touches=4,
        min_span_bars=20, brk=0.0015, cooldown=5, near_bars=10, near_tol=0.01)
    assert out == []
