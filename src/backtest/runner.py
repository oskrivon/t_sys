"""Strategy runners: adapt different strategy types into universal Trade lists.

Three adapters:
  - CandleStrategy: iterate candles, SL/TP exit (Miro-style)
  - EventStrategy: discrete events with known entry/exit (funding capture)
  - PortfolioStrategy: daily weight targets → rebalance trades (volume ranking)

Usage:
    trades = run_backtest(MyStrategy(), data, cost_model=bybit_futures())
    metrics = compute_metrics(trades)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from .cost import CostModel
from .models import ExitReason, Side, Trade


# ---------------------------------------------------------------------------
# Candle-based (Miro-style)
# ---------------------------------------------------------------------------

class CandleStrategy(ABC):
    """Iterate candles, detect entries, simulate SL/TP exit.

    Subclass and implement ``generate_signals``. The default ``simulate_exit``
    walks candles checking SL/TP hit. Override for custom exit logic.
    """

    @abstractmethod
    def generate_signals(self, symbol: str, df: pd.DataFrame) -> list[dict]:
        """Return signal dicts, each with at minimum:
        entry_idx, entry_price, side ("long"/"short"), sl, tp.
        Optional: metadata dict, leverage.
        """
        ...

    def simulate_exit(
        self,
        df: pd.DataFrame,
        signal: dict,
        max_hold: int = 200,
    ) -> dict:
        """Walk candles from entry checking SL/TP. Returns exit info dict."""
        entry_idx = signal["entry_idx"]
        is_long = signal["side"] == "long"
        sl = signal["sl"]
        tp = signal["tp"]

        for i in range(entry_idx + 1, min(entry_idx + max_hold + 1, len(df))):
            h = df["high"].iloc[i]
            lo = df["low"].iloc[i]

            if is_long:
                if lo <= sl:
                    return {"exit_idx": i, "exit_price": sl, "reason": "sl"}
                if h >= tp:
                    return {"exit_idx": i, "exit_price": tp, "reason": "tp"}
            else:
                if h >= sl:
                    return {"exit_idx": i, "exit_price": sl, "reason": "sl"}
                if lo <= tp:
                    return {"exit_idx": i, "exit_price": tp, "reason": "tp"}

        # Timeout
        exit_idx = min(entry_idx + max_hold, len(df) - 1)
        return {
            "exit_idx": exit_idx,
            "exit_price": df["close"].iloc[exit_idx],
            "reason": "timeout",
        }

    def run(
        self,
        data: dict[str, pd.DataFrame],
        position_size: float = 1000.0,
        max_hold: int = 200,
        **params: Any,
    ) -> list[Trade]:
        trades: list[Trade] = []
        for symbol, df in data.items():
            if len(df) < 10:
                continue
            signals = self.generate_signals(symbol, df)
            for sig in signals:
                exit_info = self.simulate_exit(df, sig, max_hold=max_hold)
                entry_ts = _get_ts(df, sig["entry_idx"])
                exit_ts = _get_ts(df, exit_info["exit_idx"])
                trades.append(Trade(
                    symbol=symbol,
                    side=Side(sig["side"]),
                    entry_time=entry_ts,
                    exit_time=exit_ts,
                    entry_price=sig["entry_price"],
                    exit_price=exit_info["exit_price"],
                    size_usd=position_size,
                    exit_reason=ExitReason(exit_info["reason"]),
                    leverage=sig.get("leverage", 1.0),
                    strategy_id=sig.get("strategy_id", ""),
                    metadata=sig.get("metadata", {}),
                ))
        return trades


# ---------------------------------------------------------------------------
# Event-based (funding capture, token unlock, etc.)
# ---------------------------------------------------------------------------

class EventStrategy(ABC):
    """Discrete events with known entry/exit times and prices.

    Subclass and implement ``find_events``.
    """

    @abstractmethod
    def find_events(self, data: dict[str, Any], **params: Any) -> list[dict]:
        """Return event dicts, each with:
        symbol, side ("long"/"short"), entry_time, exit_time,
        entry_price, exit_price.
        Optional: size_usd, leverage, exit_reason, metadata.
        """
        ...

    def run(self, data: dict[str, Any], **params: Any) -> list[Trade]:
        events = self.find_events(data, **params)
        trades: list[Trade] = []
        for ev in events:
            trades.append(Trade(
                symbol=ev["symbol"],
                side=Side(ev["side"]),
                entry_time=ev["entry_time"],
                exit_time=ev["exit_time"],
                entry_price=ev["entry_price"],
                exit_price=ev["exit_price"],
                size_usd=ev.get("size_usd", params.get("position_size", 1000.0)),
                exit_reason=ExitReason(ev.get("exit_reason", "signal")),
                leverage=ev.get("leverage", 1.0),
                strategy_id=ev.get("strategy_id", ""),
                metadata=ev.get("metadata", {}),
            ))
        return trades


# ---------------------------------------------------------------------------
# Portfolio / weight-based (volume ranking, pairs trading)
# ---------------------------------------------------------------------------

class PortfolioStrategy(ABC):
    """Daily target weights → rebalance trades.

    Subclass and implement ``compute_weights``.
    Positive weight = long, negative = short.
    """

    @abstractmethod
    def compute_weights(
        self,
        data: dict[str, pd.DataFrame],
        date: pd.Timestamp,
    ) -> dict[str, float]:
        """Return {symbol: signed_weight}. Sum of abs(weights) <= 1.0."""
        ...

    def run(
        self,
        data: dict[str, pd.DataFrame],
        total_capital: float = 10_000.0,
        **params: Any,
    ) -> list[Trade]:
        """Convert daily weight changes into discrete trades."""
        # Find common date range
        all_dates: set[pd.Timestamp] = set()
        for df in data.values():
            if "ts" in df.columns:
                dates = pd.to_datetime(df["ts"]).dt.normalize().unique()
            else:
                dates = df.index.normalize().unique()
            all_dates.update(dates)

        sorted_dates = sorted(all_dates)
        if len(sorted_dates) < 2:
            return []

        prev_weights: dict[str, float] = {}
        trades: list[Trade] = []

        for i, date in enumerate(sorted_dates):
            weights = self.compute_weights(data, date)

            # Find price for each symbol on this date
            prices: dict[str, float] = {}
            for sym, df in data.items():
                price = _get_price_at_date(df, date)
                if price is not None:
                    prices[sym] = price

            # Generate trades from weight changes
            all_symbols = set(prev_weights) | set(weights)
            for sym in all_symbols:
                if sym not in prices:
                    continue
                old_w = prev_weights.get(sym, 0.0)
                new_w = weights.get(sym, 0.0)
                delta = new_w - old_w

                if abs(delta) < 1e-8:
                    continue

                # Close old position if direction flipped or reduced
                if old_w != 0.0 and (
                    (old_w > 0 and delta < 0) or (old_w < 0 and delta > 0)
                ):
                    # Closing trade
                    close_size = min(abs(delta), abs(old_w)) * total_capital
                    exit_date = date
                    entry_date = sorted_dates[max(0, i - 1)]
                    trades.append(Trade(
                        symbol=sym,
                        side=Side.LONG if old_w > 0 else Side.SHORT,
                        entry_time=_ts(entry_date),
                        exit_time=_ts(exit_date),
                        entry_price=_get_price_at_date(data.get(sym, pd.DataFrame()), entry_date) or prices[sym],
                        exit_price=prices[sym],
                        size_usd=close_size,
                        exit_reason=ExitReason.REBALANCE,
                    ))

                # Open new position if direction changed or increased
                if new_w != 0.0 and abs(new_w) > abs(old_w):
                    open_size = (abs(new_w) - abs(old_w)) * total_capital
                    # This trade will be closed on next rebalance
                    # We record entry now; exit when weight changes again

            prev_weights = dict(weights)

        # Close all remaining positions at last date
        last_date = sorted_dates[-1]
        for sym, w in prev_weights.items():
            if abs(w) < 1e-8:
                continue
            price = _get_price_at_date(data.get(sym, pd.DataFrame()), last_date)
            if price is None:
                continue
            # Find when this position was opened (last weight change)
            trades.append(Trade(
                symbol=sym,
                side=Side.LONG if w > 0 else Side.SHORT,
                entry_time=_ts(sorted_dates[-2] if len(sorted_dates) > 1 else last_date),
                exit_time=_ts(last_date),
                entry_price=price,
                exit_price=price,
                size_usd=abs(w) * total_capital,
                exit_reason=ExitReason.REBALANCE,
            ))

        return trades


# ---------------------------------------------------------------------------
# Universal entry point
# ---------------------------------------------------------------------------

def run_backtest(
    strategy: CandleStrategy | EventStrategy | PortfolioStrategy,
    data: dict[str, Any],
    cost_model: Optional[CostModel] = None,
    **params: Any,
) -> list[Trade]:
    """Run strategy on data, apply cost model, return costed trades."""
    raw_trades = strategy.run(data, **params)
    if cost_model is not None:
        cost_model.apply_all(raw_trades)
    return raw_trades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ts(df: pd.DataFrame, idx: int) -> datetime:
    """Extract datetime from DataFrame row."""
    if "ts" in df.columns:
        val = df["ts"].iloc[idx]
        if isinstance(val, pd.Timestamp):
            return val.to_pydatetime()
        if isinstance(val, datetime):
            return val
        return pd.Timestamp(val).to_pydatetime()
    # Fall back to index
    val = df.index[idx]
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime()
    return datetime.now(timezone.utc)


def _get_price_at_date(df: pd.DataFrame, date: pd.Timestamp) -> Optional[float]:
    """Get close price for a given date."""
    if df.empty:
        return None
    if "ts" in df.columns:
        mask = pd.to_datetime(df["ts"]).dt.normalize() == date
        if mask.any():
            return float(df.loc[mask, "close"].iloc[-1])
    return None


def _ts(date: pd.Timestamp) -> datetime:
    """Convert pandas Timestamp to datetime."""
    if isinstance(date, pd.Timestamp):
        return date.to_pydatetime()
    return datetime.now(timezone.utc)
