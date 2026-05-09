"""Pairs Trading / Statistical Arbitrage Strategy.

Z-score mean reversion on price ratio of correlated pairs.
Market neutral: long one leg, short the other.

Entry: |z| > z_entry (ratio diverged from mean)
Exit:  z crosses zero (mean reversion) or |z| > z_stop (divergence continues)

Each 4h tick:
  1. Fetch recent candles for all pair symbols
  2. Compute rolling ratio + z-score
  3. Check for entry/exit signals
  4. Emit TradeSignal with metadata indicating pair trade
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import structlog

from src.strategies.base import (
    Strategy, StrategyConfig, StrategyType, TradeSignal, Side,
)

logger = structlog.get_logger()

# OOS-validated pairs (from backtest_pairs_trading.py)
DEFAULT_PAIRS = [
    ("BTC/USDT:USDT", "LTC/USDT:USDT"),    # Sharpe 0.90, WR 68%
    ("DOT/USDT:USDT", "FIL/USDT:USDT"),     # Sharpe 0.70, WR 60%
    ("FIL/USDT:USDT", "LTC/USDT:USDT"),     # Sharpe 0.52, WR 58%
    ("AVAX/USDT:USDT", "OP/USDT:USDT"),     # Sharpe 0.48, WR 68%
    ("DOT/USDT:USDT", "NEAR/USDT:USDT"),    # Sharpe 0.34, WR 62%
]


class PairsTradingStrategy(Strategy):
    """Z-score mean reversion on correlated pairs."""

    def __init__(self, config: StrategyConfig, state=None):
        super().__init__(config)
        self._state = state

        # Params from config
        self._z_entry: float = config.params.get("z_entry", 2.0)
        self._z_exit: float = config.params.get("z_exit", 0.0)
        self._z_stop: float = config.params.get("z_stop", 4.0)
        self._lookback: int = config.params.get("lookback", 80)
        self._max_hold_candles: int = config.params.get("max_hold_candles", 120)
        self._candle_limit: int = self._lookback + 20  # fetch enough history

        # Parse pairs
        pairs_cfg = config.params.get("pairs", None)
        if pairs_cfg:
            self._pairs = [(p[0], p[1]) for p in pairs_cfg]
        else:
            self._pairs = DEFAULT_PAIRS

        # State per pair
        self._positions: dict[str, dict] = {}  # pair_key -> {direction, entry_z, entry_idx, ...}
        self._candle_count: dict[str, int] = {}  # pair_key -> candles since entry

    def _pair_key(self, sym_a: str, sym_b: str) -> str:
        return f"{sym_a}|{sym_b}"

    async def initialize(self, exchange) -> None:
        logger.info("pairs_trading_init",
                     pairs=len(self._pairs),
                     z_entry=self._z_entry,
                     z_stop=self._z_stop,
                     lookback=self._lookback)

    async def on_tick(self, exchange) -> list[TradeSignal]:
        """Called every 4h. Fetch candles, compute z-scores, generate signals."""
        signals = []

        for sym_a, sym_b in self._pairs:
            try:
                sig = await self._check_pair(exchange, sym_a, sym_b)
                if sig:
                    signals.extend(sig)
            except Exception:
                logger.exception("pairs_check_error", pair=f"{sym_a}/{sym_b}")

        return signals

    async def _check_pair(
        self, exchange, sym_a: str, sym_b: str
    ) -> Optional[list[TradeSignal]]:
        """Check one pair for entry/exit signals."""
        pair_key = self._pair_key(sym_a, sym_b)

        # Fetch 4h candles for both symbols
        candles_a = await exchange.fetch_ohlcv(sym_a, "4h", limit=self._candle_limit)
        candles_b = await exchange.fetch_ohlcv(sym_b, "4h", limit=self._candle_limit)

        if not candles_a or not candles_b:
            return None

        # Align by timestamp
        closes_a = {c[0]: c[4] for c in candles_a}  # ts -> close
        closes_b = {c[0]: c[4] for c in candles_b}
        common_ts = sorted(set(closes_a.keys()) & set(closes_b.keys()))

        if len(common_ts) < self._lookback + 5:
            return None

        prices_a = np.array([closes_a[ts] for ts in common_ts])
        prices_b = np.array([closes_b[ts] for ts in common_ts])

        # Compute z-score of ratio
        ratio = prices_a / prices_b
        lookback = self._lookback
        r_mean = np.mean(ratio[-lookback:])
        r_std = np.std(ratio[-lookback:])
        if r_std < 1e-10:
            return None

        z = (ratio[-1] - r_mean) / r_std
        current_price_a = prices_a[-1]
        current_price_b = prices_b[-1]

        logger.info("pairs_zscore",
                     pair=f"{sym_a.split('/')[0]}/{sym_b.split('/')[0]}",
                     z=round(z, 2),
                     ratio=round(ratio[-1], 6))

        # Track candle count for open positions
        if pair_key in self._positions:
            self._candle_count[pair_key] = self._candle_count.get(pair_key, 0) + 1

        # --- EXIT CHECK ---
        if pair_key in self._positions:
            pos = self._positions[pair_key]
            hold = self._candle_count.get(pair_key, 0)
            direction = pos["direction"]

            should_exit = False
            exit_reason = ""

            if direction == 1 and z >= self._z_exit:
                should_exit = True
                exit_reason = "mean_reversion"
            elif direction == -1 and z <= self._z_exit:
                should_exit = True
                exit_reason = "mean_reversion"
            elif direction == 1 and z < -self._z_stop:
                should_exit = True
                exit_reason = "stop_divergence"
            elif direction == -1 and z > self._z_stop:
                should_exit = True
                exit_reason = "stop_divergence"
            elif hold >= self._max_hold_candles:
                should_exit = True
                exit_reason = "timeout"

            if should_exit:
                logger.info("pairs_exit_signal",
                             pair=pair_key, reason=exit_reason,
                             z=round(z, 2), hold_candles=hold)

                # Generate close signals for both legs
                exit_signals = self._make_exit_signals(
                    sym_a, sym_b, direction, current_price_a, current_price_b,
                    z, exit_reason, pos,
                )
                del self._positions[pair_key]
                self._candle_count.pop(pair_key, None)
                return exit_signals

            return None  # position open, no exit yet

        # --- ENTRY CHECK ---
        if abs(z) > self._z_entry:
            direction = 1 if z < -self._z_entry else -1
            # direction=1: long ratio (long A, short B) — z is low, ratio will rise
            # direction=-1: short ratio (short A, long B) — z is high, ratio will fall

            self._positions[pair_key] = {
                "direction": direction,
                "entry_z": z,
                "entry_price_a": current_price_a,
                "entry_price_b": current_price_b,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            self._candle_count[pair_key] = 0

            logger.info("pairs_entry_signal",
                         pair=f"{sym_a.split('/')[0]}/{sym_b.split('/')[0]}",
                         direction="long_ratio" if direction == 1 else "short_ratio",
                         z=round(z, 2))

            return self._make_entry_signals(
                sym_a, sym_b, direction, current_price_a, current_price_b, z,
            )

        return None

    def _make_entry_signals(
        self, sym_a: str, sym_b: str, direction: int,
        price_a: float, price_b: float, z: float,
    ) -> list[TradeSignal]:
        """Create entry signals for both legs."""
        pair_label = f"{sym_a.split('/')[0]}/{sym_b.split('/')[0]}"

        if direction == 1:  # long ratio: long A, short B
            side_a, side_b = Side.LONG, Side.SHORT
        else:  # short ratio: short A, long B
            side_a, side_b = Side.SHORT, Side.LONG

        meta = {
            "type": "pairs_trade",
            "action": "entry",
            "pair": pair_label,
            "direction": "long_ratio" if direction == 1 else "short_ratio",
            "z_score": round(z, 3),
            "leg": None,  # filled per signal
        }

        return [
            TradeSignal(
                strategy_id=self.config.strategy_id,
                symbol=sym_a,
                side=side_a,
                entry_price=price_a,
                sl=0.0,  # managed by z-score, not price SL
                tp=0.0,
                confidence=min(abs(z) / 4.0, 1.0),
                metadata={**meta, "leg": "A"},
            ),
            TradeSignal(
                strategy_id=self.config.strategy_id,
                symbol=sym_b,
                side=side_b,
                entry_price=price_b,
                sl=0.0,
                tp=0.0,
                confidence=min(abs(z) / 4.0, 1.0),
                metadata={**meta, "leg": "B"},
            ),
        ]

    def _make_exit_signals(
        self, sym_a: str, sym_b: str, direction: int,
        price_a: float, price_b: float, z: float,
        exit_reason: str, entry_pos: dict,
    ) -> list[TradeSignal]:
        """Create exit signals (close both legs)."""
        pair_label = f"{sym_a.split('/')[0]}/{sym_b.split('/')[0]}"

        # Exit = opposite side to close
        if direction == 1:  # was long A, short B → close = short A, long B
            side_a, side_b = Side.SHORT, Side.LONG
        else:
            side_a, side_b = Side.LONG, Side.SHORT

        meta = {
            "type": "pairs_trade",
            "action": "exit",
            "pair": pair_label,
            "exit_reason": exit_reason,
            "entry_z": entry_pos["entry_z"],
            "exit_z": round(z, 3),
            "leg": None,
        }

        return [
            TradeSignal(
                strategy_id=self.config.strategy_id,
                symbol=sym_a,
                side=side_a,
                entry_price=price_a,
                sl=0.0,
                tp=0.0,
                metadata={**meta, "leg": "A", "reduce_only": True},
            ),
            TradeSignal(
                strategy_id=self.config.strategy_id,
                symbol=sym_b,
                side=side_b,
                entry_price=price_b,
                sl=0.0,
                tp=0.0,
                metadata={**meta, "leg": "B", "reduce_only": True},
            ),
        ]

    def get_status(self) -> dict:
        base = super().get_status()
        base["open_pairs"] = len(self._positions)
        base["pairs"] = [
            f"{a.split('/')[0]}/{b.split('/')[0]}"
            for a, b in self._pairs
        ]
        return base
