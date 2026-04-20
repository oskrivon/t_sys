"""TradingEngine — main daemon. Owns event loop and all components."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
import yaml

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
        redis_url: str = "",
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        strategies_config: str = "config/strategies.yml",
    ) -> None:
        self._api_key = bybit_api_key
        self._api_secret = bybit_api_secret
        self._total_capital = total_capital
        self._redis_url = redis_url
        self._tg_token = telegram_token
        self._tg_chat_id = telegram_chat_id
        self._strategies_config = strategies_config

        # Components (initialized in start())
        self.event_bus = EventBus()
        self.ws: Optional[BybitWebSocket] = None
        self.exchange = None
        self.positions = PositionTracker()
        self.executor: Optional[ExecutionManager] = None
        self.scheduler: Optional[StrategyScheduler] = None
        self.state: Optional[StateManager] = None
        self.redis_bus = None
        self.notifier = None

        self._strategies: dict[str, Strategy] = {}
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        logger.info("engine_starting", capital=self._total_capital)

        # 1. State manager
        self.state = StateManager()

        # 2. Redis bus (optional but recommended)
        if self._redis_url:
            from src.core.redis_bus import RedisBus
            self.redis_bus = RedisBus(self._redis_url)
            await self.redis_bus.connect()

        # 3. Telegram notifier (direct — for when Redis not available)
        if self._tg_token and self._tg_chat_id:
            from src.api.telegram.bot import TelegramNotifier
            self.notifier = TelegramNotifier(self._tg_token, self._tg_chat_id)

        await self._notify("Engine starting...")

        # 4. Exchange (CCXT async)
        import ccxt.async_support as ccxt
        self.exchange = ccxt.bybit({
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "enableRateLimit": True,
        })
        await self.exchange.load_markets()
        logger.info("exchange_connected", markets=len(self.exchange.markets))

        # 5. Sync positions
        raw_positions = await self.exchange.fetch_positions()
        self.positions.sync_from_exchange(raw_positions)
        logger.info("positions_synced", count=self.positions.count)

        # 6. Execution manager
        self.executor = ExecutionManager(
            exchange=self._make_adapter(),
            event_bus=self.event_bus,
            positions=self.positions,
            notifier=self.notifier,
        )
        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self.executor._on_signal)
        self.event_bus.subscribe(EventType.ORDER_UPDATE, self.executor._on_order_update)

        # Publish trade events to Redis
        if self.redis_bus:
            self.event_bus.subscribe(EventType.POSITION_OPENED, self._on_position_event)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_event)

        # 7. Register strategies from YAML config
        await self._register_strategies()

        # 8. WebSocket
        self.ws = BybitWebSocket(
            event_bus=self.event_bus,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        await self.ws.connect()

        all_symbols = self._collect_symbols()
        await self.ws.subscribe_tickers(all_symbols)
        await self.ws.subscribe_executions()
        logger.info("ws_subscribed", symbols=len(all_symbols))

        # Give funding strategy WS ref
        funding = self._strategies.get("funding_capture")
        if funding and isinstance(funding, FundingCaptureStrategy):
            funding.set_ws(self.ws)

        # 9. Scheduler for non-WS strategies
        scheduled = {k: v for k, v in self._strategies.items()
                     if v.config.params.get("schedule")}
        self.scheduler = StrategyScheduler(
            strategies=scheduled,
            event_bus=self.event_bus,
            exchange=self._make_adapter(),
        )

        # 10. Subscribe to Redis commands
        if self.redis_bus:
            from src.core.redis_bus import CH_COMMANDS_ENGINE
            self.redis_bus.on(CH_COMMANDS_ENGINE, self._on_command)

        # 11. Run
        logger.info("engine_started",
                     strategies=list(self._strategies.keys()))
        await self._notify("Engine started. Strategies: " +
                            ", ".join(self._strategies.keys()))

        try:
            tasks = [
                self.event_bus.run(),
                self.ws.run(),
                self.state.periodic_save(
                    positions_fn=self.positions.to_dict, interval=60),
                self._health_check_loop(),
                self._wait_shutdown(),
            ]
            if scheduled:
                tasks.append(self.scheduler.run())
            if self.redis_bus:
                tasks.append(self.redis_bus.run())

            async with asyncio.TaskGroup() as tg:
                for t in tasks:
                    tg.create_task(t)
        except* KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("engine_shutting_down")

        if self.scheduler:
            await self.scheduler.stop()
        if self.executor:
            await self.executor.cancel_pending_exits()
        if self.ws:
            await self.ws.disconnect()
        await self.event_bus.stop()

        if self.state:
            self.state.save_positions(self.positions.to_dict())
            self.state.stop()
            self.state.close()

        if self.redis_bus:
            await self.redis_bus.stop()
            await self.redis_bus.disconnect()

        if self.exchange:
            await self.exchange.close()

        await self._notify("Engine stopped.")
        logger.info("engine_stopped")

    # ------------------------------------------------------------------
    # Strategy registration from YAML
    # ------------------------------------------------------------------

    async def _register_strategies(self) -> None:
        """Load strategies from config/strategies.yml."""
        config_path = Path(self._strategies_config)
        if not config_path.exists():
            logger.warning("strategies_config_not_found", path=str(config_path))
            return

        with open(config_path) as f:
            raw = yaml.safe_load(f)

        for sid, cfg in raw.get("strategies", {}).items():
            if not cfg.get("enabled", False):
                continue

            stype = StrategyType(cfg.get("type", "event_driven"))
            config = StrategyConfig(
                strategy_id=sid,
                name=sid.replace("_", " ").title(),
                strategy_type=stype,
                allocation_pct=cfg.get("allocation_pct", 10.0),
                max_positions=cfg.get("max_positions", 5),
                params=cfg.get("params", {}),
            )

            strategy = self._create_strategy(sid, config)
            if strategy:
                await strategy.initialize(self.exchange)
                self._strategies[sid] = strategy
                logger.info("strategy_registered", name=sid)

    def _create_strategy(self, sid: str, config: StrategyConfig) -> Optional[Strategy]:
        """Factory: create strategy instance by ID."""
        if sid == "funding_capture":
            return FundingCaptureStrategy(config, self.event_bus)
        # Add more strategies here as they become ready:
        # if sid == "miro_breakout":
        #     return MiroStrategy(config)
        # if sid == "volume_ranking":
        #     return VolumeRankingStrategy(config)
        logger.warning("unknown_strategy", strategy_id=sid)
        return None

    def _collect_symbols(self) -> list[str]:
        symbols = set(["BTCUSDT", "ETHUSDT"])
        for strategy in self._strategies.values():
            if isinstance(strategy, FundingCaptureStrategy):
                symbols.update(strategy._monitored)
        return list(symbols)

    # ------------------------------------------------------------------
    # Redis command handling
    # ------------------------------------------------------------------

    async def _on_command(self, message: dict) -> None:
        """Handle commands from Telegram bot via Redis."""
        payload = message.get("payload", {})
        cmd = payload.get("command", "")
        strategy_id = payload.get("strategy_id", "")

        logger.info("engine_command", command=cmd, strategy=strategy_id)

        if cmd == "status":
            await self._publish_status()
        elif cmd == "start" and strategy_id:
            await self._notify(f"Start {strategy_id}: not yet implemented (restart engine)")
        elif cmd == "stop" and strategy_id:
            await self._notify(f"Stop {strategy_id}: not yet implemented (restart engine)")
        elif cmd == "positions":
            await self._publish_positions()

    async def _publish_status(self) -> None:
        """Write engine status to Redis KV + send notification."""
        status = {
            "running": True,
            "strategies": list(self._strategies.keys()),
            "positions": self.positions.count,
            "ws_connected": self.ws.is_connected if self.ws else False,
            "uptime_check": datetime.now(timezone.utc).isoformat(),
        }
        status_text = (
            f"Engine: running\n"
            f"Strategies: {', '.join(self._strategies.keys())}\n"
            f"Positions: {self.positions.count}\n"
            f"WS: {'connected' if status.get('ws_connected') else 'disconnected'}"
        )
        if self.redis_bus:
            await self.redis_bus.set("engine:status", status_text, ex=600)
        await self._notify(status_text)

    async def _publish_positions(self) -> None:
        """Write positions to Redis KV + send notification."""
        positions = self.positions.get_all()
        if not positions:
            text = "No open positions"
        else:
            lines = []
            for p in positions:
                lines.append(f"{p.symbol} {p.side} qty={p.qty} @ {p.entry_price}")
            text = "\n".join(lines)
        if self.redis_bus:
            await self.redis_bus.set("engine:positions", text, ex=600)
        await self._notify(text)

    async def _on_position_event(self, event: Event) -> None:
        """Forward position events to Redis."""
        if self.redis_bus:
            from src.core.redis_bus import CH_EVENTS_TRADES, CH_NOTIFICATIONS_TG
            await self.redis_bus.publish(CH_EVENTS_TRADES, event.data, source="engine")
            # Also notify
            data = event.data
            if event.type == EventType.POSITION_OPENED:
                text = f"OPEN: {data.get('side', '').upper()} {data.get('symbol', '')} ({data.get('strategy', '')})"
            else:
                text = f"CLOSE: {data.get('symbol', '')} ({data.get('strategy', '')})"
            await self.redis_bus.publish(CH_NOTIFICATIONS_TG, {"text": text}, source="engine")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_adapter(self):
        from src.core.exchange.ccxt_adapter import CCXTAdapter
        from src.core.models import Exchange
        adapter = CCXTAdapter.__new__(CCXTAdapter)
        adapter.exchange = Exchange.BYBIT
        adapter._client = self.exchange
        return adapter

    async def _health_check_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(300)
            if self.ws and not self.ws.is_connected:
                logger.error("health_ws_disconnected")
                await self._notify("WARNING: WebSocket disconnected!")
            if self.executor:
                await self.executor.sync_positions()
            # Update Redis status
            if self.redis_bus:
                pos_count = self.positions.count
                await self.redis_bus.set("engine:status",
                    f"running | {len(self._strategies)} strategies | {pos_count} positions",
                    ex=600)
                bal_text = f"${self._total_capital} (config)"
                await self.redis_bus.set("engine:balance", bal_text, ex=600)

    async def _wait_shutdown(self) -> None:
        await self._shutdown_event.wait()

    async def _notify(self, text: str) -> None:
        # Prefer Redis (telegram bot forwards), fallback to direct
        if self.redis_bus:
            try:
                from src.core.redis_bus import CH_NOTIFICATIONS_TG
                await self.redis_bus.publish(CH_NOTIFICATIONS_TG, {"text": text}, source="engine")
                return
            except Exception:
                pass
        if self.notifier:
            try:
                await self.notifier.send_text(text)
            except Exception:
                logger.warning("notify_failed", text=text[:50])
