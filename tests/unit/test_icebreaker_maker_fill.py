"""Unit tests for the maker fill-risk simulator."""
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


mf = _load("icebreaker_maker_fill", "scripts/research/icebreaker_maker_fill.py")
es = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")

CFG = {"buffer": 0.001, "tp1": 0.005, "f1": 0.5, "trail_giveback": 0.005,
       "horizon_ms": 10_000}


# ---- find_fill -------------------------------------------------------------
def test_long_limit_fills_on_dip_to_limit():
    # post a buy limit at 99.5; price dips to 99.4 at index 2 -> fill there
    ts = np.array([0, 100, 200, 300], dtype="int64")
    px = np.array([100.0, 99.8, 99.4, 99.0])
    f = mf.find_fill(ts, px, 0, "long", 99.5, 10_000)
    assert f == (2, 200)


def test_long_limit_no_fill_if_price_runs_away():
    # buy limit at 99.5 but price only goes UP -> never filled (the adverse-selection
    # case: the runner never comes back)
    ts = np.array([0, 100, 200], dtype="int64")
    px = np.array([100.0, 100.5, 101.0])
    assert mf.find_fill(ts, px, 0, "long", 99.5, 10_000) is None


def test_short_limit_fills_on_rise_to_limit():
    ts = np.array([0, 100, 200], dtype="int64")
    px = np.array([100.0, 100.3, 100.6])
    f = mf.find_fill(ts, px, 0, "short", 100.5, 10_000)
    assert f == (2, 200)


def test_fill_respects_entry_window():
    # touch happens at t=5000 but window is 1000ms -> no fill
    ts = np.array([0, 5000], dtype="int64")
    px = np.array([100.0, 99.0])
    assert mf.find_fill(ts, px, 0, "long", 99.5, 1_000) is None
    assert mf.find_fill(ts, px, 0, "long", 99.5, 6_000) == (1, 5000)


# ---- walk_exit equivalence to the validated simulate_exit ------------------
def test_walk_exit_matches_simulate_exit_on_taker_entry():
    ts = np.array([0, 100, 200, 300, 400], dtype="int64")
    px = np.array([100.0, 100.5, 101.0, 100.7, 100.49])
    cfg_es = {**CFG, "fee_side": 0.0}
    ref = es.simulate_exit(ts, px, 0, "long", 99.9, cfg_es)
    got = mf.walk_exit(ts, px, 0, float(px[0]), "long", 99.9, CFG)
    assert abs(got["gross"] - ref["gross"]) < 1e-12
    assert got["reason"] == ref["reason"]
    assert abs(got["mfe"] - ref["mfe"]) < 1e-12
    # exit_units + 1 entry unit == simulate_exit's fee_units
    assert abs((got["exit_units"] + 1.0) - ref["fee_units"]) < 1e-12


def test_net_pnl_charges_entry_and_exit_legs():
    res = {"gross": 0.01, "exit_units": 1.0}
    # entry 1 unit @ maker 2bps + exit 1 unit @ taker 5.5bps
    assert abs(mf.net_pnl(res, 0.0002, 0.00055) - (0.01 - 0.0002 - 0.00055)) < 1e-12


def test_maker_better_fill_price_raises_gross_vs_taker():
    # filling lower (maker buy at 99.5 vs taker at 100.0) yields more gross on the
    # same upward path
    ts = np.array([0, 100, 200, 300], dtype="int64")
    px = np.array([99.5, 100.0, 100.6, 101.0])
    taker = mf.walk_exit(ts, px, 1, 100.0, "long", 99.4, CFG)   # entered at 100.0
    maker = mf.walk_exit(ts, px, 0, 99.5, "long", 99.4, CFG)    # filled at 99.5
    assert maker["gross"] > taker["gross"]
