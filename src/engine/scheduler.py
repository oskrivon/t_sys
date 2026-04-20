"""Strategy scheduler — runs strategies on their configured schedule.

FundingCapture: WS-driven (not scheduled here).
Miro: every 4h aligned to candle close.
VolumeRanking: daily at 00:05 UTC.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog

from src.engine.event_bus import EventBus, Event, EventType
from src.strategies.base import Strategy, StrategyType, StrategyConfig

logger = structlog.get_logger()


class StrategyScheduler:
    """Runs non-WS strategies on time-aligned schedules."""

    def __init__(
        self,
        strategies: dict[str, Strategy],
        event_bus: EventBus,
        exchange,
    ) -> None:
        self._strategies = strategies
        self._event_bus = event_bus
        self._exchange = exchange
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        self._running = True
        for sid, strategy in self._strategies.items():
            cron = strategy.config.params.get("schedule")
            if cron == "4h":
                self._tasks.append(asyncio.create_task(
                    self._run_periodic(strategy, hours=4, offset_secs=60)
                ))
            elif cron == "1d":
                self._tasks.append(asyncio.create_task(
                    self._run_daily(strategy, hour_utc=0, minute_utc=5)
                ))
            # funding_capture has no schedule — WS-driven
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()

    async def _run_periodic(self, strategy: Strategy, hours: int, offset_secs: int = 60) -> None:
        """Run strategy aligned to Nh boundaries (e.g. 00:00, 04:00, 08:00 for 4h)."""
        while self._running:
            now = datetime.now(timezone.utc)
            # Next boundary
            current_hour = now.hour
            next_hour = ((current_hour // hours) + 1) * hours
            if next_hour >= 24:
                next_run = (now + timedelta(days=1)).replace(
                    hour=next_hour % 24, minute=0, second=0, microsecond=0)
            else:
                next_run = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)

            sleep_secs = (next_run - now).total_seconds() + offset_secs
            logger.info("scheduler_waiting",
                         strategy=strategy.config.strategy_id,
                         next_run=next_run.isoformat(),
                         sleep_secs=int(sleep_secs))
            await asyncio.sleep(sleep_secs)

            if not self._running:
                break

            await self._tick_strategy(strategy)

    async def _run_daily(self, strategy: Strategy, hour_utc: int, minute_utc: int) -> None:
        """Run strategy once daily at specified UTC time."""
        while self._running:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=hour_utc, minute=minute_utc, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_secs = (target - now).total_seconds()
            logger.info("scheduler_waiting",
                         strategy=strategy.config.strategy_id,
                         next_run=target.isoformat(),
                         sleep_secs=int(sleep_secs))
            await asyncio.sleep(sleep_secs)

            if not self._running:
                break

            await self._tick_strategy(strategy)

    async def _tick_strategy(self, strategy: Strategy) -> None:
        """Execute one strategy tick and route results."""
        sid = strategy.config.strategy_id
        try:
            logger.info("strategy_tick_start", strategy=sid)
            results = await strategy.on_tick(self._exchange)

            if not results:
                logger.info("strategy_tick_no_signals", strategy=sid)
                return

            if strategy.config.strategy_type == StrategyType.EVENT_DRIVEN:
                for signal in results:
                    await self._event_bus.publish(Event(
                        type=EventType.SIGNAL_GENERATED,
                        data=signal,
                        source=sid,
                    ))
                logger.info("strategy_tick_signals",
                             strategy=sid, count=len(results))
            elif strategy.config.strategy_type == StrategyType.SYSTEMATIC:
                # Publish rebalance event — executor handles it
                await self._event_bus.publish(Event(
                    type=EventType.SIGNAL_GENERATED,
                    data={"strategy_id": sid, "targets": results, "type": "rebalance"},
                    source=sid,
                ))
                logger.info("strategy_tick_rebalance",
                             strategy=sid, targets=len(results))

        except Exception:
            logger.exception("strategy_tick_error", strategy=sid)
