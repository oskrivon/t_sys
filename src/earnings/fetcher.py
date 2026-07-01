"""Data fetcher: earnings calendar, prices, and transcripts."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import structlog
import yfinance as yf

from .models import EarningsEvent

logger = structlog.get_logger()

# ── S&P 100 constituents (OEX, as of 2025) ───────────────────────────

SP100_SYMBOLS = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT",
    "AMZN", "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY",
    "BRK-B", "C", "CAT", "CHTR", "CL", "CMCSA", "COF", "COP", "COST",
    "CRM", "CSCO", "CVS", "CVX", "DE", "DHR", "DIS", "DOW", "DUK",
    "EMR", "EXC", "F", "FDX", "GD", "GE", "GILD", "GM", "GOOG",
    "GS", "HD", "HON", "IBM", "INTC", "INTU", "JNJ", "JPM", "KHC",
    "KO", "LIN", "LLY", "LMT", "LOW", "MA", "MCD", "MDLZ", "MDT",
    "MET", "META", "MMM", "MO", "MRK", "MS", "MSFT", "NEE", "NFLX",
    "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG", "PM", "PYPL", "QCOM",
    "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TMO", "TMUS",
    "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WBA", "WFC",
    "WMT", "XOM",
]

SP500_SYMBOLS: list[str] = []  # TODO: populate if needed


def get_universe(name: str) -> list[str]:
    """Return list of ticker symbols for a given universe."""
    if name == "sp100":
        return SP100_SYMBOLS
    if name == "sp500":
        if not SP500_SYMBOLS:
            raise NotImplementedError(
                "S&P 500 list not populated yet. Use sp100 or add symbols."
            )
        return SP500_SYMBOLS
    raise ValueError(f"Unknown universe: {name}")


# ── Earnings calendar ─────────────────────────────────────────────────

def fetch_earnings_calendar_yf(
    symbols: list[str],
    start: str,
    end: str,
    cache_dir: str = "data/cache/earnings/calendar",
) -> list[EarningsEvent]:
    """Fetch earnings dates and EPS data from yfinance.

    yfinance provides: earnings date, EPS estimate, EPS actual, surprise%.
    Caches per-symbol JSON to avoid re-fetching.
    """
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    events: list[EarningsEvent] = []
    failed: list[str] = []

    for i, symbol in enumerate(symbols):
        cache_file = cache_path / f"{symbol}.json"

        # Use cache if fresh (< 7 days old)
        if cache_file.exists():
            age_days = (time.time() - cache_file.stat().st_mtime) / 86400
            if age_days < 7:
                try:
                    cached = json.loads(cache_file.read_text())
                    for row in cached:
                        ed = date.fromisoformat(row["earnings_date"])
                        if start_dt <= ed <= end_dt:
                            events.append(EarningsEvent(**row))
                    continue
                except Exception:
                    pass  # re-fetch on cache corruption

        # Fetch from yfinance
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.get_earnings_dates(limit=50)
            if df is None or df.empty:
                logger.debug("no_earnings_data", symbol=symbol)
                failed.append(symbol)
                continue

            rows: list[dict[str, Any]] = []
            for idx, row in df.iterrows():
                ed = idx.date() if hasattr(idx, "date") else idx
                if not isinstance(ed, date):
                    continue

                eps_est = _safe_float(row.get("EPS Estimate"))
                eps_act = _safe_float(row.get("Reported EPS"))
                surprise = _safe_float(row.get("Surprise(%)"))

                # Compute surprise if not provided
                if surprise is None and eps_est and eps_act and eps_est != 0:
                    surprise = ((eps_act - eps_est) / abs(eps_est)) * 100

                entry = {
                    "symbol": symbol,
                    "earnings_date": ed.isoformat(),
                    "report_time": "amc",  # yfinance doesn't provide this, default conservative
                    "eps_estimate": eps_est,
                    "eps_actual": eps_act,
                    "eps_surprise_pct": surprise,
                }
                rows.append(entry)

                if start_dt <= ed <= end_dt:
                    events.append(EarningsEvent(**entry))

            # Cache all rows (not just filtered)
            cache_file.write_text(json.dumps(rows, indent=2))

        except Exception as e:
            logger.warning("earnings_fetch_error", symbol=symbol, error=str(e))
            failed.append(symbol)

        # Rate-limit: yfinance can throttle
        if (i + 1) % 20 == 0:
            logger.info("earnings_fetch_progress", done=i + 1, total=len(symbols))
            time.sleep(1)

    if failed:
        logger.warning("earnings_fetch_failed", symbols=failed, count=len(failed))

    logger.info(
        "earnings_calendar_done",
        events=len(events),
        symbols_ok=len(symbols) - len(failed),
        symbols_failed=len(failed),
    )
    return events


def fetch_earnings_calendar_fmp(
    symbols: list[str],
    start: str,
    end: str,
    api_key: str,
    cache_dir: str = "data/cache/earnings/calendar",
) -> list[EarningsEvent]:
    """Fetch earnings calendar from Financial Modeling Prep API.

    Fallback source with better report_time (bmo/amc) data.
    """
    import requests

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    usage = _load_fmp_usage(cache_dir)

    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    events: list[EarningsEvent] = []

    # FMP earnings calendar is date-range based, not per-symbol
    cache_file = cache_path / f"fmp_{start}_{end}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            symbol_set = set(symbols)
            for row in cached:
                if row["symbol"] in symbol_set:
                    ed = date.fromisoformat(row["earnings_date"])
                    if start_dt <= ed <= end_dt:
                        events.append(EarningsEvent(**row))
            logger.info("fmp_cache_hit", events=len(events))
            return events
        except Exception:
            pass

    if not api_key:
        raise ValueError("FMP API key required. Set fmp_api_key in config.")

    # Fetch quarterly chunks to stay within limits
    all_rows: list[dict[str, Any]] = []
    symbol_set = set(symbols)
    current = start_dt

    while current <= end_dt:
        chunk_end = min(current + timedelta(days=90), end_dt)
        if usage["count"] >= 250:
            logger.warning("fmp_daily_limit_reached", fetched=len(all_rows))
            break

        url = (
            f"https://financialmodelingprep.com/api/v3/earning_calendar"
            f"?from={current.isoformat()}&to={chunk_end.isoformat()}"
            f"&apikey={api_key}"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        usage["count"] += 1
        data = resp.json()

        for item in data:
            sym = item.get("symbol", "")
            if sym not in symbol_set:
                continue

            ed_str = item.get("date", "")
            if not ed_str:
                continue

            report_time = "amc"
            fmp_time = item.get("time", "")
            if fmp_time and "bmo" in fmp_time.lower():
                report_time = "bmo"
            elif fmp_time and "amc" in fmp_time.lower():
                report_time = "amc"

            eps_est = _safe_float(item.get("epsEstimated"))
            eps_act = _safe_float(item.get("eps"))
            rev_est = _safe_float(item.get("revenueEstimated"))
            rev_act = _safe_float(item.get("revenue"))

            surprise = None
            if eps_est is not None and eps_act is not None and eps_est != 0:
                surprise = ((eps_act - eps_est) / abs(eps_est)) * 100

            rev_surprise = None
            if rev_est and rev_act and rev_est != 0:
                rev_surprise = ((rev_act - rev_est) / abs(rev_est)) * 100

            row = {
                "symbol": sym,
                "earnings_date": ed_str,
                "report_time": report_time,
                "eps_estimate": eps_est,
                "eps_actual": eps_act,
                "eps_surprise_pct": surprise,
                "revenue_estimate": rev_est,
                "revenue_actual": rev_act,
                "revenue_surprise_pct": rev_surprise,
            }
            all_rows.append(row)

        current = chunk_end + timedelta(days=1)
        time.sleep(0.5)

    # Cache and filter
    cache_file.write_text(json.dumps(all_rows, indent=2))
    _save_fmp_usage(cache_dir, usage)

    for row in all_rows:
        ed = date.fromisoformat(row["earnings_date"])
        if start_dt <= ed <= end_dt:
            events.append(EarningsEvent(**row))

    logger.info("fmp_calendar_done", events=len(events), api_calls=usage["count"])
    return events


# ── Price data ────────────────────────────────────────────────────────

def fetch_prices(
    symbols: list[str],
    start: str,
    end: str,
    cache_dir: str = "data/cache/earnings/prices",
) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV for all symbols via yfinance. Cache as parquet."""
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    result: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []

    # Check cache first
    for symbol in symbols:
        pq = cache_path / f"{symbol}.parquet"
        if pq.exists():
            age_days = (time.time() - pq.stat().st_mtime) / 86400
            if age_days < 7:
                try:
                    df = pd.read_parquet(pq)
                    result[symbol] = df
                    continue
                except Exception:
                    pass
        to_fetch.append(symbol)

    if to_fetch:
        logger.info("fetching_prices", count=len(to_fetch))
        # Batch download — yfinance supports multiple tickers
        # Extend date range slightly for hold period buffer
        end_extended = (
            datetime.strptime(end, "%Y-%m-%d") + timedelta(days=30)
        ).strftime("%Y-%m-%d")

        for batch_start in range(0, len(to_fetch), 50):
            batch = to_fetch[batch_start : batch_start + 50]
            try:
                data = yf.download(
                    batch,
                    start=start,
                    end=end_extended,
                    interval="1d",
                    progress=False,
                    group_by="ticker" if len(batch) > 1 else "column",
                    auto_adjust=True,
                )

                if len(batch) == 1:
                    sym = batch[0]
                    if not data.empty:
                        df = _normalize_price_df(data)
                        df.to_parquet(cache_path / f"{sym}.parquet")
                        result[sym] = df
                else:
                    for sym in batch:
                        try:
                            if sym in data.columns.get_level_values(0):
                                df = data[sym].dropna(how="all")
                                if not df.empty:
                                    df = _normalize_price_df(df)
                                    df.to_parquet(cache_path / f"{sym}.parquet")
                                    result[sym] = df
                        except Exception as e:
                            logger.debug("price_extract_error", symbol=sym, error=str(e))

            except Exception as e:
                logger.warning("price_batch_error", batch=batch[:5], error=str(e))

            time.sleep(1)

    logger.info("prices_done", loaded=len(result), missing=len(symbols) - len(result))
    return result


# ── Transcript fetcher (Phase 2) ─────────────────────────────────────

def fetch_transcript_fmp(
    symbol: str,
    year: int,
    quarter: int,
    api_key: str,
    cache_dir: str = "data/cache/earnings/transcripts",
) -> Optional[str]:
    """Fetch earnings call transcript from FMP. Returns full text or None."""
    import requests

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    cache_file = cache_path / f"{symbol}_{year}_Q{quarter}.txt"
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8")
        return text if text.strip() else None

    if not api_key:
        return None

    usage = _load_fmp_usage(str(cache_path.parent))
    if usage["count"] >= 250:
        logger.debug("fmp_limit_reached_transcript", symbol=symbol)
        return None

    try:
        url = (
            f"https://financialmodelingprep.com/api/v3/earning_call_transcript"
            f"/{symbol}?quarter={quarter}&year={year}&apikey={api_key}"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        usage["count"] += 1
        _save_fmp_usage(str(cache_path.parent), usage)

        data = resp.json()
        if not data:
            cache_file.write_text("")  # mark as empty
            return None

        # FMP returns list with single item containing "content"
        content = data[0].get("content", "") if isinstance(data, list) else ""
        cache_file.write_text(content, encoding="utf-8")
        return content if content.strip() else None

    except Exception as e:
        logger.warning("transcript_fetch_error", symbol=symbol, error=str(e))
        return None


def truncate_transcript(text: str, max_words: int = 5000) -> str:
    """Truncate transcript keeping prepared remarks + Q&A start.

    Typical structure:
    1. Operator intro
    2. Prepared remarks (CEO, CFO)
    3. Q&A session
    """
    words = text.split()
    if len(words) <= max_words:
        return text

    # Try to find Q&A section boundary
    lower = text.lower()
    qa_markers = [
        "question-and-answer",
        "question and answer",
        "q&a session",
        "operator: thank you",
        "our first question",
    ]

    qa_pos = -1
    for marker in qa_markers:
        pos = lower.find(marker)
        if pos > 0:
            qa_pos = pos
            break

    if qa_pos > 0:
        # Keep all prepared remarks + first part of Q&A
        prepared = text[:qa_pos]
        qa_text = text[qa_pos:]
        prepared_words = prepared.split()
        qa_words = qa_text.split()

        # Allocate: 60% prepared, 40% Q&A
        prep_limit = int(max_words * 0.6)
        qa_limit = max_words - min(len(prepared_words), prep_limit)

        result_parts = []
        if len(prepared_words) > prep_limit:
            result_parts.append(" ".join(prepared_words[:prep_limit]))
            result_parts.append("\n\n[... prepared remarks truncated ...]\n\n")
        else:
            result_parts.append(prepared)

        result_parts.append(" ".join(qa_words[:qa_limit]))
        if len(qa_words) > qa_limit:
            result_parts.append("\n\n[... Q&A truncated ...]")

        return "".join(result_parts)

    # No Q&A marker found — just truncate
    return " ".join(words[:max_words]) + "\n\n[... truncated ...]"


# ── Helpers ───────────────────────────────────────────────────────────

def _safe_float(val: Any) -> Optional[float]:
    """Convert to float, returning None for NaN/None/missing."""
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _normalize_price_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize yfinance DataFrame columns to lowercase."""
    df = df.copy()
    # Handle MultiIndex columns from yfinance
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(-1)
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    # Ensure index is datetime
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df.index = df.index.tz_localize(None)
    df = df.sort_index()
    return df


def _fmp_usage_file(cache_dir: str) -> Path:
    return Path(cache_dir) / ".fmp_usage.json"


def _load_fmp_usage(cache_dir: str) -> dict[str, Any]:
    """Load FMP daily usage counter."""
    f = _fmp_usage_file(cache_dir)
    today = date.today().isoformat()
    if f.exists():
        try:
            data = json.loads(f.read_text())
            if data.get("date") == today:
                return data
        except Exception:
            pass
    return {"date": today, "count": 0}


def _save_fmp_usage(cache_dir: str, usage: dict[str, Any]) -> None:
    f = _fmp_usage_file(cache_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(usage))
