"""Predictor data fetching and computation."""
from __future__ import annotations

import asyncio
import time

import pandas as pd
import structlog

from src.weekend.config import PredictorDef, SignalType
from src.weekend.ensemble import PredictorResult

logger = structlog.get_logger()


class InsufficientDataError(Exception):
    """Raised when required market data is missing."""


def fetch_ticker_data(
    ticker: str,
    start: str,
    end: str,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> pd.DataFrame:
    """Fetch daily OHLC from yfinance with retry.

    Args:
        ticker: yfinance ticker symbol (e.g. "KWEB", "USDJPY=X").
        start: Start date "YYYY-MM-DD".
        end: End date "YYYY-MM-DD" (exclusive in yfinance).
        retries: Number of retry attempts.
        retry_delay: Base delay between retries (exponential backoff).

    Returns:
        DataFrame with columns: date, open, high, low, close, volume.
        date column is tz-naive datetime.

    Raises:
        InsufficientDataError: If no data after all retries.
    """
    import yfinance as yf

    last_error = None
    for attempt in range(retries):
        try:
            df = yf.download(
                ticker, start=start, end=end,
                interval="1d", progress=False,
            )
            if len(df) == 0:
                raise InsufficientDataError(f"No data returned for {ticker}")

            df = df.reset_index()
            df.columns = [
                c.lower() if isinstance(c, str) else c[0].lower()
                for c in df.columns
            ]
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            df["weekday"] = df["date"].dt.weekday
            return df

        except InsufficientDataError:
            raise
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                delay = retry_delay * (2 ** attempt)
                logger.warning(
                    "yfinance_retry",
                    ticker=ticker, attempt=attempt + 1,
                    error=str(e), next_delay=delay,
                )
                time.sleep(delay)

    raise InsufficientDataError(
        f"Failed to fetch {ticker} after {retries} attempts: {last_error}"
    )


def compute_friday_return(df: pd.DataFrame) -> float:
    """Compute Friday open-to-close return in percent.

    Raises:
        InsufficientDataError: If no Friday data found.
    """
    fridays = df[df["weekday"] == 4]
    if len(fridays) == 0:
        raise InsufficientDataError("No Friday data in DataFrame")

    last_friday = fridays.iloc[-1]
    open_price = last_friday["open"]
    if open_price == 0:
        raise InsufficientDataError("Friday open price is zero")

    return (last_friday["close"] - open_price) / open_price * 100


def compute_week_return(df: pd.DataFrame) -> float:
    """Compute Monday open to Friday close return in percent.

    Raises:
        InsufficientDataError: If Monday or Friday data missing.
    """
    mondays = df[df["weekday"] == 0]
    fridays = df[df["weekday"] == 4]

    if len(mondays) == 0:
        raise InsufficientDataError("No Monday data in DataFrame")
    if len(fridays) == 0:
        raise InsufficientDataError("No Friday data in DataFrame")

    mon_open = mondays.iloc[-1]["open"]
    fri_close = fridays.iloc[-1]["close"]

    if mon_open == 0:
        raise InsufficientDataError("Monday open price is zero")

    return (fri_close - mon_open) / mon_open * 100


def compute_predictor(
    predictor: PredictorDef,
    target_friday: pd.Timestamp,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> PredictorResult | None:
    """Fetch data and compute a single predictor signal.

    Returns None if data fetch fails (logged, not raised).
    """
    from datetime import timedelta

    start = (target_friday - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (target_friday + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = fetch_ticker_data(
            predictor.ticker, start, end,
            retries=retries, retry_delay=retry_delay,
        )

        if predictor.signal_type == SignalType.FRIDAY_RETURN:
            value_pct = compute_friday_return(df)
        elif predictor.signal_type == SignalType.WEEK_RETURN:
            value_pct = compute_week_return(df)
        else:
            raise ValueError(f"Unknown signal type: {predictor.signal_type}")

        if predictor.invert:
            value_pct = -value_pct

        vote = 1 if value_pct > 0 else -1

        return PredictorResult(
            name=predictor.name,
            ticker=predictor.ticker,
            value_pct=value_pct,
            vote=vote,
        )

    except Exception as e:
        logger.error(
            "predictor_failed",
            predictor=predictor.name, ticker=predictor.ticker,
            error=str(e),
        )
        return None
