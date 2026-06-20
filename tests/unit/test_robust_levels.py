"""Unit tests for robust level + squeeze detection."""
from __future__ import annotations

import numpy as np

from src.strategy import robust_levels as rl


def test_atr_basic():
    h = np.array([10, 11, 12, 13.0])
    l = np.array([9, 10, 11, 12.0])
    c = np.array([9.5, 10.5, 11.5, 12.5])
    a = rl.atr(h, l, c, period=2)
    assert np.isnan(a[0])
    assert a[-1] > 0


def test_volume_profile_poc():
    # most volume traded at price 100 -> POC near 100
    price = np.array([100, 100, 100, 105, 110.0])
    qty = np.array([10, 10, 10, 1, 1.0])
    centers, mass = rl.volume_profile(price, qty, bins=10)
    poc = centers[mass.argmax()]
    assert abs(poc - 100) < 1.5


def test_volume_at_concentrated():
    centers = np.array([99.0, 100.0, 101.0])
    mass = np.array([1.0, 8.0, 1.0])
    frac = rl.volume_at(centers, mass, 100.0, half_width=0.5)
    assert abs(frac - 0.8) < 1e-9


def test_find_pivots_strict():
    h = np.array([1, 2, 5, 2, 1, 2, 6, 2, 1.0])
    l = -h
    piv = rl.find_pivots(h, l, order=2)
    highs = [idx for idx, _, k in piv if k == "high"]
    assert 2 in highs and 6 in highs


def test_cluster_pivots_groups_within_proximity():
    piv = [(0, 100.0, "high"), (5, 100.2, "high"), (9, 110.0, "high")]
    cl = rl.cluster_pivots(piv, proximity=0.5)
    prices = sorted(round(c["price"], 1) for c in cl)
    assert prices == [100.1, 110.0]
    big = [c for c in cl if c["price"] < 105][0]
    assert big["n"] == 2


def test_ttm_squeeze_fires_in_low_vol():
    rng = np.random.RandomState(0)
    # 60 bars: first 30 wide/volatile, last 30 very tight -> squeeze in tail
    close = np.concatenate([100 + rng.randn(30) * 3, 100 + rng.randn(30) * 0.05])
    high = close + 0.1
    low = close - 0.1
    sq = rl.ttm_squeeze(high, low, close, length=20)
    assert sq[-5:].any()        # squeeze detected in the tight tail
    assert not sq[25]           # not during the volatile section


def _make_base_then_breakout():
    """480 bars: long history below 100, tight squeezed base hugging just under
    100 with repeated highs at 100, then a decisive breakout bar above 100."""
    bars = []
    for i in range(470):
        # drift well below the level, tightening near the end
        base = 99.0 if i < 350 else 99.7
        amp = 0.5 if i < 350 else 0.05
        hi = min(base + amp + (0.3 if (i >= 440 and i % 5 == 0) else 0), 100.0)
        bars.append({"ts": i * 60000, "open": base, "high": hi,
                     "low": base - amp, "close": base, "volume": 100.0})
    # decisive breakout
    bars.append({"ts": 470 * 60000, "open": 99.8, "high": 101.0, "low": 99.8,
                 "close": 100.6, "volume": 500.0})
    return bars


def test_detect_setups_finds_clean_long_break():
    bars = _make_base_then_breakout()
    setups = rl.detect_setups(
        bars, lookback=460, base_bars=100, pivot_order=3, atr_period=14,
        cluster_atr_mult=1.0, min_touches=3, brk=0.001, require_squeeze=False,
        one_sided_min=0.7, near_atr=5.0, cooldown=10)
    assert len(setups) >= 1
    s = setups[0]
    assert s.side == "long"
    assert abs(s.level - 100.0) / 100.0 < 0.01
    assert s.one_sided >= 0.7


def test_detect_setups_rejects_midrange_chop():
    # price oscillates symmetrically around 100 -> no one-sided base, no setup
    rng = np.random.RandomState(1)
    bars = []
    for i in range(500):
        c = 100 + rng.randn() * 1.0
        bars.append({"ts": i * 60000, "open": c, "high": c + 0.5,
                     "low": c - 0.5, "close": c, "volume": 100.0})
    setups = rl.detect_setups(bars, lookback=460, base_bars=100, min_touches=3,
                              require_squeeze=False, one_sided_min=0.8, near_atr=2.0)
    # symmetric chop -> one-sidedness can't clear 0.8 -> essentially nothing
    assert len(setups) <= 2
