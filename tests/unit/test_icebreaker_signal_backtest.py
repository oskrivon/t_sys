"""Unit tests for the faithful absorption signal + TP/SL sim."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# load the script module (scripts/ isn't a package)
_spec = importlib.util.spec_from_file_location(
    "icebreaker_signal_backtest",
    Path(__file__).resolve().parents[2] / "scripts" / "research"
    / "icebreaker_signal_backtest.py")
sbt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sbt   # dataclass needs the module registered
_spec.loader.exec_module(sbt)


def _wall_then_gone(price=100.0, size=1500.0):
    """Book rows: snapshot with a bid wall, then a delta removing it."""
    return [
        (1000, 1, "snapshot", "bid", price, size),
        (1000, 1, "snapshot", "ask", price + 1, 1.0),
        (2000, 2, "delta", "bid", price, 0.0),   # wall vanishes at ts=2000
    ]


def test_absorbed_wall_emits_signal():
    rows = _wall_then_gone()
    # sells at the wall price covering its size between born(1000) and end(2000)
    by_price = {round(100.0, 10): [(1500, "sell", 800.0), (1800, "sell", 800.0)]}
    sigs = sbt.find_signals(rows, by_price, min_notional=100_000, absorb_frac=1.0)
    assert len(sigs) == 1
    assert sigs[0].side == "bid"
    assert sigs[0].absorbed_qty == 1600.0


def test_pulled_wall_no_signal():
    rows = _wall_then_gone()
    by_price = {round(100.0, 10): [(1500, "sell", 100.0)]}  # only 100 < 1500
    sigs = sbt.find_signals(rows, by_price, min_notional=100_000, absorb_frac=1.0)
    assert sigs == []


def test_wrong_side_trades_dont_absorb():
    rows = _wall_then_gone()
    # buys don't eat a bid (support) wall
    by_price = {round(100.0, 10): [(1500, "buy", 5000.0)]}
    sigs = sbt.find_signals(rows, by_price, min_notional=100_000, absorb_frac=1.0)
    assert sigs == []


def test_below_threshold_never_a_wall():
    rows = [
        (1000, 1, "snapshot", "bid", 100.0, 500.0),   # 50k < 100k
        (1000, 1, "snapshot", "ask", 101.0, 1.0),
        (2000, 2, "delta", "bid", 100.0, 0.0),
    ]
    by_price = {round(100.0, 10): [(1500, "sell", 9999.0)]}
    assert sbt.find_signals(rows, by_price, 100_000, 1.0) == []


def test_partial_absorption_frac():
    rows = _wall_then_gone(size=1000.0)  # 100k notional exactly
    by_price = {round(100.0, 10): [(1500, "sell", 600.0)]}  # 60% of size
    assert sbt.find_signals(rows, by_price, 100_000, absorb_frac=1.0) == []
    assert len(sbt.find_signals(rows, by_price, 100_000, absorb_frac=0.5)) == 1


def test_simulate_tp_sl_short():
    # one bid-eaten (SHORT) signal; price falls -> TP
    sig = sbt.Signal("bid", 100.0, 1500.0, 1600.0, 1000, 2000)
    ts = [2200, 2300, 2400]
    px = [100.0, 99.4, 99.0]   # entry 100, -0.5% TP at 99.5 hit at 99.4
    res = sbt.simulate([sig], ts, px, tp=0.005, sl=0.0015,
                       entry_delay_ms=200, horizon_ms=60_000)
    assert res[0][1] == "tp"


def test_simulate_sl_long():
    sig = sbt.Signal("ask", 100.0, 1500.0, 1600.0, 1000, 2000)  # LONG
    ts = [2200, 2300]
    px = [100.0, 99.8]  # price drops -> SL (0.15% = 99.85)
    res = sbt.simulate([sig], ts, px, tp=0.005, sl=0.0015,
                       entry_delay_ms=200, horizon_ms=60_000)
    assert res[0][1] == "sl"
    assert res[0][2] < 0   # realized pnl negative


def test_simulate_open_marked_to_market():
    """A position that hits neither TP nor SL is realized at horizon price."""
    sig = sbt.Signal("ask", 100.0, 1500.0, 1600.0, 1000, 2000)  # LONG
    ts = [2200, 2300, 2400]
    px = [100.0, 100.2, 100.3]  # never hits TP(100.5) or SL(99.85)
    res = sbt.simulate([sig], ts, px, tp=0.005, sl=0.0015,
                       entry_delay_ms=200, horizon_ms=60_000)
    assert res[0][1] == "open"
    assert res[0][2] == pytest.approx((100.3 - 100.0) / 100.0)  # MTM, not zero


def test_simulate_time_exit():
    """Fixed-time exit ignores TP/SL and exits at the held-time price."""
    sig = sbt.Signal("bid", 100.0, 1500.0, 1600.0, 1000, 2000)  # SHORT
    ts = [2200, 2300, 50_000]
    px = [100.0, 99.0, 98.0]  # short, price falls -> profit
    res = sbt.simulate([sig], ts, px, tp=0.005, sl=0.0015,
                       entry_delay_ms=200, horizon_ms=60_000, time_exit_ms=30_000)
    assert res[0][1] == "time"
    # exits ~30s after entry (ts 2200+30000=32200 -> idx for 50_000)... nearest >=
    assert res[0][2] > 0   # short profited as price fell
