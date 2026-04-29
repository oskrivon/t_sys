"""Unit tests for weekend predictor computation."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.weekend.config import PredictorDef, SignalType
from src.weekend.predictors import (
    InsufficientDataError,
    compute_friday_return,
    compute_predictor,
    compute_week_return,
)


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a test DataFrame from row dicts with date, open, close."""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["weekday"] = df["date"].dt.weekday
    df["high"] = df["close"]
    df["low"] = df["open"]
    df["volume"] = 1000
    return df


# ── A realistic week: Mon 2026-04-20 to Fri 2026-04-24 ──────────────

WEEK_DATA = _make_df([
    {"date": "2026-04-20", "open": 100.0, "close": 101.0},  # Monday
    {"date": "2026-04-21", "open": 101.0, "close": 102.0},  # Tuesday
    {"date": "2026-04-22", "open": 102.0, "close": 101.5},  # Wednesday
    {"date": "2026-04-23", "open": 101.5, "close": 103.0},  # Thursday
    {"date": "2026-04-24", "open": 103.0, "close": 105.0},  # Friday
])


class TestComputeFridayReturn:
    def test_positive_return(self):
        ret = compute_friday_return(WEEK_DATA)
        # (105 - 103) / 103 * 100 ≈ 1.9417%
        assert ret == pytest.approx((105 - 103) / 103 * 100)

    def test_negative_return(self):
        df = _make_df([
            {"date": "2026-04-24", "open": 105.0, "close": 102.0},  # Friday
        ])
        ret = compute_friday_return(df)
        assert ret == pytest.approx((102 - 105) / 105 * 100)
        assert ret < 0

    def test_missing_friday_raises(self):
        df = _make_df([
            {"date": "2026-04-20", "open": 100.0, "close": 101.0},  # Monday
            {"date": "2026-04-23", "open": 101.5, "close": 103.0},  # Thursday
        ])
        with pytest.raises(InsufficientDataError, match="No Friday"):
            compute_friday_return(df)

    def test_empty_dataframe_raises(self):
        df = pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume", "weekday"])
        with pytest.raises(InsufficientDataError, match="No Friday"):
            compute_friday_return(df)

    def test_zero_open_raises(self):
        df = _make_df([
            {"date": "2026-04-24", "open": 0.0, "close": 105.0},  # Friday
        ])
        with pytest.raises(InsufficientDataError, match="zero"):
            compute_friday_return(df)

    def test_multiple_fridays_uses_latest(self):
        df = _make_df([
            {"date": "2026-04-17", "open": 90.0, "close": 92.0},   # prev Friday
            {"date": "2026-04-24", "open": 103.0, "close": 105.0},  # this Friday
        ])
        ret = compute_friday_return(df)
        assert ret == pytest.approx((105 - 103) / 103 * 100)


class TestComputeWeekReturn:
    def test_positive_week(self):
        ret = compute_week_return(WEEK_DATA)
        # (Fri close 105 - Mon open 100) / 100 * 100 = 5%
        assert ret == pytest.approx(5.0)

    def test_negative_week(self):
        df = _make_df([
            {"date": "2026-04-20", "open": 110.0, "close": 108.0},  # Monday
            {"date": "2026-04-24", "open": 103.0, "close": 100.0},  # Friday
        ])
        ret = compute_week_return(df)
        assert ret == pytest.approx((100 - 110) / 110 * 100)
        assert ret < 0

    def test_missing_monday_raises(self):
        df = _make_df([
            {"date": "2026-04-24", "open": 103.0, "close": 105.0},  # Friday only
        ])
        with pytest.raises(InsufficientDataError, match="Monday"):
            compute_week_return(df)

    def test_missing_friday_raises(self):
        df = _make_df([
            {"date": "2026-04-20", "open": 100.0, "close": 101.0},  # Monday only
        ])
        with pytest.raises(InsufficientDataError, match="Friday"):
            compute_week_return(df)

    def test_zero_monday_open_raises(self):
        df = _make_df([
            {"date": "2026-04-20", "open": 0.0, "close": 101.0},   # Monday
            {"date": "2026-04-24", "open": 103.0, "close": 105.0},  # Friday
        ])
        with pytest.raises(InsufficientDataError, match="zero"):
            compute_week_return(df)


class TestComputePredictor:
    """Test the full predictor pipeline with mocked yfinance."""

    def test_friday_predictor_bullish(self, monkeypatch):
        df = WEEK_DATA.copy()
        monkeypatch.setattr(
            "src.weekend.predictors.fetch_ticker_data",
            lambda *a, **kw: df,
        )
        pred = PredictorDef(
            name="test_fri", ticker="TEST",
            signal_type=SignalType.FRIDAY_RETURN,
        )
        result = compute_predictor(pred, pd.Timestamp("2026-04-24"))
        assert result is not None
        assert result.vote == 1
        assert result.value_pct > 0

    def test_week_predictor(self, monkeypatch):
        df = WEEK_DATA.copy()
        monkeypatch.setattr(
            "src.weekend.predictors.fetch_ticker_data",
            lambda *a, **kw: df,
        )
        pred = PredictorDef(
            name="test_week", ticker="TEST",
            signal_type=SignalType.WEEK_RETURN,
        )
        result = compute_predictor(pred, pd.Timestamp("2026-04-24"))
        assert result is not None
        assert result.vote == 1
        assert result.value_pct == pytest.approx(5.0)

    def test_inverted_predictor(self, monkeypatch):
        df = WEEK_DATA.copy()
        monkeypatch.setattr(
            "src.weekend.predictors.fetch_ticker_data",
            lambda *a, **kw: df,
        )
        pred = PredictorDef(
            name="test_inv", ticker="TEST",
            signal_type=SignalType.WEEK_RETURN,
            invert=True,
        )
        result = compute_predictor(pred, pd.Timestamp("2026-04-24"))
        assert result is not None
        assert result.vote == -1  # inverted: positive return → bearish
        assert result.value_pct < 0

    def test_fetch_failure_returns_none(self, monkeypatch):
        def _fail(*a, **kw):
            raise InsufficientDataError("no data")
        monkeypatch.setattr(
            "src.weekend.predictors.fetch_ticker_data", _fail,
        )
        pred = PredictorDef(
            name="test_fail", ticker="FAIL",
            signal_type=SignalType.FRIDAY_RETURN,
        )
        result = compute_predictor(pred, pd.Timestamp("2026-04-24"))
        assert result is None
