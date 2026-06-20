"""Unit tests for the scalper exit simulator."""
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


es = _load("icebreaker_exit_sim", "scripts/research/icebreaker_exit_sim.py")

CFG = {"buffer": 0.001, "tp1": 0.005, "f1": 0.5, "trail_giveback": 0.005,
       "horizon_ms": 10_000, "fee_side": 0.00055}


def test_stop_hit_is_negative_about_one_R():
    # long entry ~100.1, level 100, stop 99.9; price drops to 99.8 -> full stop
    ts = np.array([0, 100, 200], dtype="int64")
    px = np.array([100.1, 100.0, 99.8])
    r = es.simulate_exit(ts, px, 0, "long", 100.0, CFG)
    assert r["reason"] == "stop"
    assert r["gross"] < 0
    assert r["R"] < 0     # lost about one unit of risk


def test_runner_takes_tp1_then_trails():
    # rises to +1% (TP1 at +0.5% fires), then retraces 0.5% from peak -> trail
    ts = np.array([0, 100, 200, 300, 400], dtype="int64")
    px = np.array([100.0, 100.5, 101.0, 100.7, 100.49])
    r = es.simulate_exit(ts, px, 0, "long", 99.9, CFG)
    assert r["reason"] == "trail"
    assert r["gross"] > 0          # half at +0.5%, rest trailed off the top
    assert r["mfe"] >= 0.01


def test_horizon_mtm_when_neither_hit():
    ts = np.array([0, 100, 200], dtype="int64")
    px = np.array([100.0, 100.1, 100.2])   # drifts up, no TP/stop
    r = es.simulate_exit(ts, px, 0, "long", 99.9, CFG)
    assert r["reason"] == "horizon"
    assert r["gross"] > 0


def test_short_side_symmetric():
    # short: price falls -> favorable. entry 100, level 100.1, stop 100.2
    ts = np.array([0, 100, 200], dtype="int64")
    px = np.array([100.0, 99.5, 99.0])   # TP1 at -0.5% fires
    r = es.simulate_exit(ts, px, 0, "short", 100.1, CFG)
    assert r["gross"] > 0
    assert r["mfe"] > 0
