"""Unit tests for breakout detection / MFE measurement / book features / AUC."""
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


bs = _load("icebreaker_breakout_study", "scripts/research/icebreaker_breakout_study.py")
ba = _load("icebreaker_breakout_analyze", "scripts/research/icebreaker_breakout_analyze.py")


def _consol_then_break(level=100.0, n=12, up=True, bar_ms=1000):
    """n flat bars touching `level`, then a bar closing through it."""
    bars = []
    for i in range(n):
        bars.append({"ts": i * bar_ms, "open": level - 0.5,
                     "high": level if up else level + 0.5,
                     "low": level - 0.5 if up else level,
                     "close": level - 0.3, "volume": 1.0})
    brk = level * 1.01 if up else level * 0.99
    bars.append({"ts": n * bar_ms, "open": level, "high": max(brk, level),
                 "low": min(brk, level), "close": brk, "volume": 5.0})
    return bars


def test_detect_long_breakout():
    bars = _consol_then_break(up=True)
    out = bs.detect_breakouts(bars, 1000, consol_bars=12, range_thresh=0.05,
                              brk=0.001, tol=0.002, min_touches=2, cooldown=6)
    assert len(out) == 1
    assert out[0]["side"] == "long"
    assert out[0]["level"] == 100.0
    assert out[0]["vol_burst"] > 1.0   # breakout bar volume > consol average


def test_detect_short_breakout():
    bars = _consol_then_break(up=False)
    out = bs.detect_breakouts(bars, 1000, consol_bars=12, range_thresh=0.05,
                              brk=0.001, tol=0.002, min_touches=2, cooldown=6)
    assert len(out) == 1 and out[0]["side"] == "short"


def test_no_breakout_when_range_too_wide():
    bars = [{"ts": i * 1000, "open": 100 + i, "high": 105 + i, "low": 95 + i,
             "close": 100 + i, "volume": 1.0} for i in range(13)]
    out = bs.detect_breakouts(bars, 1000, consol_bars=12, range_thresh=0.02,
                              brk=0.001, tol=0.002, min_touches=2, cooldown=6)
    assert out == []


def test_measure_move_long():
    ts = [1000, 1100, 1200, 1300]
    px = [100.0, 102.0, 99.0, 101.0]  # entry 100; hi 102 (+2%), lo 99 (-1%)
    entry, mfe, mae, t_peak = bs.measure_move(ts, px, 1000, "long", 10_000)
    assert entry == 100.0
    assert abs(mfe - 0.02) < 1e-9
    assert abs(mae - 0.01) < 1e-9
    assert t_peak == 100  # peak at ts=1100


def test_measure_move_short_inverts():
    ts = [1000, 1100, 1200]
    px = [100.0, 98.0, 101.0]  # short: favorable is down -> mfe from 98 (+2%)
    entry, mfe, mae, _ = bs.measure_move(ts, px, 1000, "short", 10_000)
    assert abs(mfe - 0.02) < 1e-9
    assert abs(mae - 0.01) < 1e-9


def test_book_features_long():
    book = bs.BookState()
    book.bids = {99.0: 100.0, 98.0: 50.0}
    book.asks = {100.0: 200.0, 100.4: 300.0, 101.0: 10.0}
    # long break through 100: wall at 100 within tol; through-depth 100..100.5
    f = bs.book_features_at(book, 100.0, "long", tol=0.002, band=0.005)
    assert f["wall_notional"] == 100.0 * 200.0
    assert f["through_depth"] == 100.0 * 200.0 + 100.4 * 300.0  # 101.0 excluded
    assert f["imbalance"] < 0  # asks heavier than bids


def test_auc_separation():
    # positives strictly above negatives -> AUC 1.0
    assert ba.auc([5, 6, 7], [1, 2, 3]) == 1.0
    # identical distributions -> 0.5
    assert ba.auc([1, 2, 3], [1, 2, 3]) == 0.5
