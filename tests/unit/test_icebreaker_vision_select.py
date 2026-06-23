"""Unit tests for the vision-selection parser + analysis helpers."""
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


vs = _load("icebreaker_vision_select", "scripts/research/icebreaker_vision_select.py")


def test_parse_clean_json():
    r = vs.parse_vision('{"score": 8, "decision": "trade", "reason": "clean break"}')
    assert r == {"score": 8, "decision": "trade", "reason": "clean break"}


def test_parse_fenced_json():
    txt = '```json\n{"score": 3, "decision": "skip", "reason": "choppy"}\n```'
    r = vs.parse_vision(txt)
    assert r["score"] == 3 and r["decision"] == "skip"


def test_parse_regex_fallback_on_malformed():
    txt = 'I think {"score": 9, "decision": "trade", "reason": "runner"} yes'
    r = vs.parse_vision(txt)
    assert r["score"] == 9 and r["decision"] == "trade"


def test_decision_defaults_from_score_when_absent():
    # no decision field -> derive from score threshold (>=7 trade)
    assert vs.parse_vision('{"score": 8, "reason": "x"}')["decision"] == "trade"
    assert vs.parse_vision('{"score": 4, "reason": "x"}')["decision"] == "skip"


def test_parse_none_when_no_score():
    assert vs.parse_vision('{"decision": "trade"}') is None
    assert vs.parse_vision("garbage") is None


def test_auc_perfect_and_random():
    # perfectly separable: positives all rank above negatives -> AUC 1.0
    assert abs(vs.auc([5, 6, 7], [1, 2, 3]) - 1.0) < 1e-9
    # fully overlapping ties -> 0.5
    assert abs(vs.auc([5, 5], [5, 5]) - 0.5) < 1e-9


def test_net_of_charges_fee_per_unit():
    r = {"gross": 0.01, "fee_units": 2.0}
    assert abs(vs.net_of(r, 0.00055) - (0.01 - 0.00055 * 2.0)) < 1e-12
