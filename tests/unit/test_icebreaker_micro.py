"""Unit tests for the dynamic micro-feature primitives."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")


def test_band_mask():
    pr = np.array([99.0, 100.0, 100.4, 101.0, 50.0])
    m = mc.band_mask(pr, [100.0], 0.005)  # +/-0.5% of 100 -> [99.5,100.5]
    assert list(m) == [False, True, True, False, False]


def test_latest_per_price():
    # price 100 seen at sizes 10 then 5 (ts order); price 101 at 7
    price = np.array([100.0, 101.0, 100.0])
    qty = np.array([10.0, 7.0, 5.0])
    uniq, last = mc.latest_per_price(price, qty)
    d = dict(zip(np.round(uniq, 6), last))
    assert d[100.0] == 5.0   # latest size at 100
    assert d[101.0] == 7.0


def test_resting_dynamics_removed_and_refill():
    # size 100 -> 0 (removed 100) -> 90 (refill after >50% drop) at price 100
    price = np.array([100.0, 100.0, 100.0])
    qty = np.array([100.0, 0.0, 90.0])
    max_wall, removed, refills = mc.resting_dynamics(price, qty, 100.0, 0.0015)
    assert max_wall == 100.0 * 100.0
    assert removed == 100.0 * 100.0
    assert refills == 1


def test_static_book_long():
    # asks: wall at 100 (size 200), supply 100..100.5
    price = np.array([100.0, 100.4, 101.0])
    qty = np.array([200.0, 300.0, 10.0])
    side = np.array([mc.ASK, mc.ASK, mc.ASK], dtype="int8")
    ts = np.array([1, 2, 3], dtype="int64")
    wall, through = mc.static_book(price, qty, side, ts, 100.0, True, 10,
                                  tol=0.002, band=0.005)
    assert wall == 100.0 * 200.0
    assert through == 100.0 * 200.0 + 100.4 * 300.0   # 101 excluded


def test_dyn_features_absorption():
    # long break at level 100. Ask wall 100 size 100 -> eaten by buys 100.
    s_ts = np.array([10, 20], dtype="int64")
    s_pr = np.array([100.0, 100.0])
    s_qty = np.array([100.0, 0.0])           # wall fully removed
    s_side = np.array([mc.ASK, mc.ASK], dtype="int8")
    surv = (s_ts, s_pr, s_qty, s_side)
    # trades: buys totaling size 100 at price 100 within window
    t_ts = np.array([15, 18], dtype="int64")
    t_pr = np.array([100.0, 100.0])
    t_qty = np.array([60.0, 40.0])
    t_side = np.array([mc.BUY, mc.BUY], dtype="int8")
    trades = (t_ts, t_pr, t_qty, t_side)
    b = {"level": 100.0, "side": "long", "ts_close": 25}
    cfg = {"window_ms": 100, "tol": 0.002, "band": 0.005}
    f = mc.dyn_features(surv, trades, b, cfg)
    assert abs(f["eaten"] - 100.0 * 100.0) < 1e-6      # 100 notional eaten
    assert f["cancelled"] == 0.0                        # all removed was eaten
    assert abs(f["absorbed_frac"] - 1.0) < 1e-6         # wall fully absorbed


def test_dyn_features_cancelled_not_eaten():
    # wall removed but NO trades -> all cancelled (the pulled-wall case)
    s_ts = np.array([10, 20], dtype="int64")
    s_pr = np.array([100.0, 100.0])
    s_qty = np.array([100.0, 0.0])
    s_side = np.array([mc.ASK, mc.ASK], dtype="int8")
    surv = (s_ts, s_pr, s_qty, s_side)
    empty = np.empty(0)
    trades = (empty.astype("int64"), empty, empty, empty.astype("int8"))
    b = {"level": 100.0, "side": "long", "ts_close": 25}
    cfg = {"window_ms": 100, "tol": 0.002, "band": 0.005}
    f = mc.dyn_features(surv, trades, b, cfg)
    assert f["eaten"] == 0.0
    assert f["cancelled"] == 100.0 * 100.0
    assert f["cancel_eat"] == 999.0     # removed but nothing eaten -> sentinel
