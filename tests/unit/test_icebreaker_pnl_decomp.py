"""Unit test for the PnL decomposition helper."""
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


pd = _load("icebreaker_pnl_decomp", "scripts/research/icebreaker_pnl_decomp.py")


def test_decomp_runs_and_sets_net(capsys):
    rs = [
        {"gross": 0.02, "fee_units": 2.0, "reason_exit": "trail", "mfe": 0.03},
        {"gross": -0.01, "fee_units": 2.0, "reason_exit": "stop", "mfe": 0.001},
        {"gross": 0.001, "fee_units": 2.0, "reason_exit": "horizon", "mfe": 0.004},
    ]
    pd.decomp("T", rs, 0.00055, worst_k=1)
    # net must be computed as gross - fee*fee_units
    assert abs(rs[0]["net"] - (0.02 - 0.00055 * 2.0)) < 1e-12
    out = capsys.readouterr().out
    assert "by exit reason" in out and "win/loss" in out and "runner capture" in out
