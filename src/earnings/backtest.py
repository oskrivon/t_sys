"""Earnings PEAD backtest engine.

Phase 1: Quantitative baseline — trade based on EPS surprise magnitude.
Phase 2: LLM-enhanced — combine EPS surprise with transcript scores.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import structlog

from src.backtest.runner import EventStrategy

from .models import EarningsEvent

logger = structlog.get_logger()


class EarningsDriftStrategy(EventStrategy):
    """PEAD strategy: enter after earnings, hold N days.

    Direction based on EPS surprise and/or LLM score.
    """

    def __init__(
        self,
        events: list[EarningsEvent],
        prices: dict[str, pd.DataFrame],
        hold_days: int = 5,
        surprise_threshold_pct: float = 5.0,
        strategy_name: str = "pead_baseline",
        llm_scores: Optional[dict[str, float]] = None,
        llm_threshold: float = 2.0,
        mode: str = "surprise_only",
    ):
        """
        Args:
            events: Earnings events with EPS surprise data.
            prices: Daily OHLCV per symbol.
            hold_days: How many trading days to hold.
            surprise_threshold_pct: Min |surprise%| to trade (Phase 1).
            strategy_name: ID for this strategy variant.
            llm_scores: {"{symbol}_{date}": composite_score} (Phase 2).
            llm_threshold: Min |llm_score| to trade (Phase 2).
            mode: "surprise_only", "llm_only", "combined", "llm_filtered".
        """
        self.events = events
        self.prices = prices
        self.hold_days = hold_days
        self.surprise_threshold_pct = surprise_threshold_pct
        self.strategy_name = strategy_name
        self.llm_scores = llm_scores or {}
        self.llm_threshold = llm_threshold
        self.mode = mode

    def find_events(self, data: dict[str, Any], **params: Any) -> list[dict]:
        """Convert earnings events into trade dicts for EventStrategy."""
        trades: list[dict] = []

        for event in self.events:
            symbol = event.symbol
            if symbol not in self.prices:
                continue

            df = self.prices[symbol]
            if df.empty:
                continue

            # Determine direction
            direction = self._get_direction(event)
            if direction is None:
                continue

            # Find entry/exit bars
            entry_info = self._find_entry(event, df)
            if entry_info is None:
                continue

            entry_time, entry_price = entry_info

            exit_info = self._find_exit(df, entry_time)
            if exit_info is None:
                continue

            exit_time, exit_price = exit_info

            # Build event key for LLM score lookup
            event_key = f"{symbol}_{event.earnings_date.isoformat()}"

            trades.append({
                "symbol": symbol,
                "side": direction,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "exit_reason": "signal",
                "strategy_id": self.strategy_name,
                "metadata": {
                    "earnings_date": event.earnings_date.isoformat(),
                    "eps_surprise_pct": event.eps_surprise_pct,
                    "llm_score": self.llm_scores.get(event_key),
                    "hold_days": self.hold_days,
                },
            })

        logger.info(
            "earnings_events_found",
            strategy=self.strategy_name,
            mode=self.mode,
            hold_days=self.hold_days,
            total_events=len(self.events),
            tradeable=len(trades),
        )
        return trades

    def _get_direction(self, event: EarningsEvent) -> Optional[str]:
        """Determine trade direction based on mode and signals."""
        surprise = event.eps_surprise_pct
        event_key = f"{event.symbol}_{event.earnings_date.isoformat()}"
        llm_score = self.llm_scores.get(event_key)

        if self.mode == "surprise_only":
            if surprise is None:
                return None
            if surprise > self.surprise_threshold_pct:
                return "long"
            if surprise < -self.surprise_threshold_pct:
                return "short"
            return None

        if self.mode == "llm_only":
            if llm_score is None:
                return None
            if llm_score > self.llm_threshold:
                return "long"
            if llm_score < -self.llm_threshold:
                return "short"
            return None

        if self.mode == "combined":
            # Both must agree on direction
            if surprise is None or llm_score is None:
                return None
            surprise_dir = (
                "long" if surprise > self.surprise_threshold_pct
                else "short" if surprise < -self.surprise_threshold_pct
                else None
            )
            llm_dir = (
                "long" if llm_score > self.llm_threshold
                else "short" if llm_score < -self.llm_threshold
                else None
            )
            if surprise_dir and llm_dir and surprise_dir == llm_dir:
                return surprise_dir
            return None

        if self.mode == "llm_filtered":
            # Take PEAD trades unless LLM disagrees
            if surprise is None:
                return None
            if surprise > self.surprise_threshold_pct:
                direction = "long"
            elif surprise < -self.surprise_threshold_pct:
                direction = "short"
            else:
                return None
            # Filter: skip if LLM strongly disagrees
            if llm_score is not None:
                if direction == "long" and llm_score < -self.llm_threshold:
                    return None
                if direction == "short" and llm_score > self.llm_threshold:
                    return None
            return direction

        return None

    def _find_entry(
        self, event: EarningsEvent, df: pd.DataFrame,
    ) -> Optional[tuple[datetime, float]]:
        """Find entry time/price respecting look-ahead bias.

        amc: entry = next trading day open
        bmo: entry = same day open (market hasn't opened yet)
        """
        earnings_date = pd.Timestamp(event.earnings_date)

        if event.report_time == "bmo":
            # Before market open — enter at same day's open
            target_date = earnings_date
        else:
            # After market close (default) — enter next day
            target_date = earnings_date + pd.Timedelta(days=1)

        # Find the first trading day >= target_date
        mask = df.index >= target_date
        if not mask.any():
            return None

        entry_idx = df.index[mask][0]
        entry_price = float(df.loc[entry_idx, "open"])
        entry_time = entry_idx.to_pydatetime()
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)

        return entry_time, entry_price

    def _find_exit(
        self, df: pd.DataFrame, entry_time: datetime,
    ) -> Optional[tuple[datetime, float]]:
        """Find exit at close of N-th trading day after entry."""
        entry_ts = pd.Timestamp(entry_time).tz_localize(None)
        mask = df.index >= entry_ts
        future = df.index[mask]

        if len(future) < self.hold_days + 1:
            return None

        # hold_days trading days after entry
        exit_idx = future[self.hold_days]
        exit_price = float(df.loc[exit_idx, "close"])
        exit_time = exit_idx.to_pydatetime()
        if exit_time.tzinfo is None:
            exit_time = exit_time.replace(tzinfo=timezone.utc)

        return exit_time, exit_price


def run_pead_backtest(
    events: list[EarningsEvent],
    prices: dict[str, pd.DataFrame],
    hold_days: int = 5,
    surprise_threshold_pct: float = 5.0,
    position_size: float = 10_000.0,
    mode: str = "surprise_only",
    llm_scores: Optional[dict[str, float]] = None,
    llm_threshold: float = 2.0,
) -> list:
    """Convenience wrapper: run PEAD backtest, return Trade list."""
    from src.backtest.models import Trade

    strategy = EarningsDriftStrategy(
        events=events,
        prices=prices,
        hold_days=hold_days,
        surprise_threshold_pct=surprise_threshold_pct,
        strategy_name=f"pead_{mode}_{hold_days}d",
        llm_scores=llm_scores,
        llm_threshold=llm_threshold,
        mode=mode,
    )
    return strategy.run({}, position_size=position_size)
