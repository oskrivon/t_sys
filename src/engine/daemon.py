"""TradingEngine — main daemon. Owns event loop and all components."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.api.telegram.bot import TelegramNotifier
from src.core.websocket.bybit_ws import BybitWebSocket
from src.engine.event_bus import EventBus, Event, EventType
from src.engine.scheduler import StrategyScheduler
from src.engine.state import StateManager
from src.execution.executor import ExecutionManager
from src.execution.position_tracker import PositionTracker
from src.strategies.base import Strategy, StrategyConfig, StrategyType
from src.strategies.funding_capture import FundingCaptureStrategy

logger = structlog.get_logger()


class TradingEngine:
    """Single-process trading daemon. Manages all components lifecycle."""

    def __init__(
        self,
        bybit_api_key: str,
        bybit_api_secret: str,
        total_capital: float = 50.0,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        # Funding capture params
        funding_enabled: bool = True,
        funding_threshold_bps: float = 10.0,
        funding_leverage: int = 10,
        # Miro params
        miro_enabled: bool = False,  # disabled until fully integrated
        # Volume Ranking params
        vr_enabled: bool = False,    # disabled until fully integrated
    ) -> None:
        self._api_key = bybit_api_key
        self._api_secret = bybit_api_secret
        self._total_capital = total_capital

        # Component refs (initialized in start())
        self.event_bus = EventBus()
        self.ws: Optional[BybitWebSocket] = None
        self.exchange = None  # CCXTAdapter
        self.positions = PositionTracker()
        self.executor: Optional[ExecutionManager] = None
        self.scheduler: Optional[StrategyScheduler] = None
        self.state: Optional[StateManager] = None
        self.notifier: Optional[TelegramNotifier] = None

        self._strategies: dict[str, Strategy] = {}
        self._shutdown_event = asyncio.Event()

        # Config
        self._tg_token = telegram_token
        self._tg_chat_id = telegram_chat_id
        self._funding_enabled = funding_enabled
        self._funding_threshold_bps = funding_threshold_bps
        self._funding_leverage = funding_leverage
        self._miro_enabled = miro_enabled
        self._vr_enabled = vr_enabled

    async def start(self) -> None:
        logger.info("engine_starting", capital=self._total_capital)

        # 1. State manager
        self.state = StateManager()

        # 2. Telegram
        if self._tg_token and self._tg_chat_id:
            self.notifier = TelegramNotifier(self._tg_token, self._tg_chat_id)
            await self._notify("Engine starting...")

        # 3. Exchange (CCXT async)
        import ccxt.async_support as ccxt
        self.exchange = ccxt.bybit({
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "enableRateLimit": True,
        })
        await self.exchange.load_markets()
        logger.info("exchange_connected", markets=len(self.exchange.markets))

        # 4. Sync positions
        raw_positions = await self.exchange.fetch_positions()
        self.positions.sync_from_exchange(raw_positions)
        logger.info("positions_synced", count=self.positions.count)

        # 5. Execution manager
        # Wrap exchange for executor (use raw ccxt client directly for speed)
        self.executor = ExecutionManager(
            exchange=self._make_adapter(),
            event_bus=self.event_bus,
            positions=self.positions,
            notifier=self.notifier,
        )
        # Subscribe to events (skip full initialize to avoid double sync)
        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self.executor._on_signal)
        self.event_bus.subscribe(EventType.ORDER_UPDATE, self.executor._on_order_update)

        # 6. Register strategies
        await self._register_strategies()

        # 7. WebSocket
        self.ws = BybitWebSocket(
            event_bus=self.event_bus,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        await self.ws.connect()

        # Subscribe to tickers for initial monitored symbols
        all_symbols = self._collect_symbols()
        await self.ws.subscribe_tickers(all_symbols)
        await self.ws.subscribe_executions()
        logger.info("ws_subscribed", symbols=len(all_symbols))

        # Give funding strategy WS ref for dynamic subscription management
        funding = self._strategies.get("funding_capture")
        if funding and isinstance(funding, FundingCaptureStrategy):
            funding.set_ws(self.ws)

        # 8. Scheduler for non-WS strategies
        scheduled = {k: v for k, v in self._strategies.items()
                     if v.config.params.get("schedule")}
        self.scheduler = StrategyScheduler(
            strategies=scheduled,
            event_bus=self.event_bus,
            exchange=self._make_adapter(),
        )

        # 9. Run all concurrent tasks
        logger.info("engine_started")
        await self._notify("Engine started. All systems go.")

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.event_bus.run())
                tg.create_task(self.ws.run())
                if scheduled:
                    tg.create_task(self.scheduler.run())
                tg.create_task(self.state.periodic_save(
                    positions_fn=self.positions.to_dict, interval=60))
                tg.create_task(self._health_check_loop())
                tg.create_task(self._wait_shutdown())
        except* KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("engine_shutting_down")

        # Stop scheduler
        if self.scheduler:
            await self.scheduler.stop()

        # Cancel pending funding exits (don't close positions)
        if self.executor:
            await self.executor.cancel_pending_exits()

        # Disconnect WS
        if self.ws:
            await self.ws.disconnect()

        # Stop event bus
        await self.event_bus.stop()

        # Save final state
        if self.state:
            self.state.save_positions(self.positions.to_dict())
            self.state.stop()
            self.state.close()

        # Close exchange
        if self.exchange:
            await self.exchange.close()

        await self._notify("Engine stopped.")
        logger.info("engine_stopped")

    # ------------------------------------------------------------------
    # Strategy registration
    # ------------------------------------------------------------------

    async def _register_strategies(self) -> None:
        if self._funding_enabled:
            config = StrategyConfig(
                strategy_id="funding_capture",
                name="Funding Capture >10bps",
                strategy_type=StrategyType.EVENT_DRIVEN,
                allocation_pct=10.0,
                max_positions=5,
                params={
                    "threshold_bps": self._funding_threshold_bps,
                    "leverage": self._funding_leverage,
                },
            )
            strategy = FundingCaptureStrategy(config, self.event_bus)
            await strategy.initialize(self.exchange)
            self._strategies["funding_capture"] = strategy
            logger.info("strategy_registered", name="funding_capture")

        # Miro and VolumeRanking integration points — add when ready
        # if self._miro_enabled: ...
        # if self._vr_enabled: ...

    def _collect_symbols(self) -> list[str]:
        """Collect all symbols that need WS subscriptions."""
        symbols = set()
        for strategy in self._strategies.values():
            if isinstance(strategy, FundingCaptureStrategy):
                symbols.update(strategy._monitored)
        # Always monitor BTC/ETH
        symbols.update(["BTCUSDT", "ETHUSDT"])
        return list(symbols)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_adapter(self):
        """Create a lightweight adapter wrapper for executor/scheduler."""
        from src.core.exchange.ccxt_adapter import CCXTAdapter
        from src.core.models import Exchange
        adapter = CCXTAdapter.__new__(CCXTAdapter)
        adapter.exchange = Exchange.BYBIT
        adapter._client = self.exchange  # set the internal attr, not the property
        return adapter

    async def _health_check_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(300)  # every 5 min
            if self.ws and not self.ws.is_connected:
                logger.error("health_ws_disconnected")
                await self._notify("WARNING: WebSocket disconnected!")
            # Periodic position sync
            if self.executor:
                await self.executor.sync_positions()

    async def _wait_shutdown(self) -> None:
        await self._shutdown_event.wait()

    async def _notify(self, text: str) -> None:
        if self.notifier:
            try:
                await self.notifier.send_text(text)
            except Exception:
                logger.warning("notify_failed", text=text[:50])
