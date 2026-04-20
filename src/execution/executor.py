"""Execution manager — order placement, position lifecycle, TP/SL management."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog

from src.core.exchange.ccxt_adapter import CCXTAdapter
from src.core.models import OrderSide, OrderType
from src.engine.event_bus import EventBus, Event, EventType
from src.execution.position_tracker import PositionTracker, LivePosition
from src.strategies.base import TradeSignal, TargetPosition, Side

logger = structlog.get_logger()


class ExecutionManager:
    """Convert signals into orders, track positions, manage exits."""

    def __init__(
        self,
        exchange: CCXTAdapter,
        event_bus: EventBus,
        positions: PositionTracker,
        notifier=None,
    ) -> None:
        self._exchange = exchange
        self._event_bus = event_bus
        self._positions = positions
        self._notifier = notifier
        self._pending_exits: dict[str, asyncio.Task] = {}
        self._executing: set[str] = set()  # symbols currently being executed (lock)
        self._funding_events: dict[str, asyncio.Event] = {}  # raw_symbol -> event for funding credited

    async def initialize(self) -> None:
        """Subscribe to events, sync positions from exchange."""
        self._event_bus.subscribe(EventType.SIGNAL_GENERATED, self._on_signal)
        self._event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)
        await self.sync_positions()
        logger.info("execution_manager_initialized",
                     open_positions=self._positions.count)

    # ------------------------------------------------------------------
    # Signal routing
    # ------------------------------------------------------------------

    async def _on_signal(self, event: Event) -> None:
        signal = event.data
        if not isinstance(signal, TradeSignal):
            return

        # Prevent duplicate execution: check both position and in-flight lock
        if self._positions.has_position(signal.symbol) or signal.symbol in self._executing:
            return

        sig_type = signal.metadata.get("type", "")
        try:
            if sig_type == "funding_capture":
                await self._execute_funding_capture(signal)
            else:
                await self._execute_event_driven(signal)
        except Exception:
            logger.exception("exec_order_failed",
                             symbol=signal.symbol,
                             strategy=signal.strategy_id)
            await self._notify(f"ORDER FAILED: {signal.symbol} ({signal.strategy_id})")

    # ------------------------------------------------------------------
    # Funding capture execution
    # ------------------------------------------------------------------

    async def _execute_funding_capture(self, signal: TradeSignal) -> None:
        # Acquire execution lock
        self._executing.add(signal.symbol)
        try:
            await self._do_funding_capture(signal)
        finally:
            self._executing.discard(signal.symbol)

    async def _do_funding_capture(self, signal: TradeSignal) -> None:
        leverage = signal.metadata.get("leverage", 10)
        exit_delay = signal.metadata.get("exit_after_seconds", 15)
        funding_bps = signal.metadata.get("funding_rate_bps", 0)

        # Set leverage
        await self._exchange.set_leverage(leverage, signal.symbol)

        # Compute qty from market info
        side = OrderSide.BUY if signal.side == Side.LONG else OrderSide.SELL
        qty = await self._compute_min_qty(signal.symbol)

        logger.info("exec_funding_entry",
                     symbol=signal.symbol,
                     side=side.value,
                     qty=str(qty),
                     leverage=leverage,
                     funding_bps=funding_bps)

        # Market order entry — use raw ccxt to avoid _parse_order issues
        t0 = asyncio.get_event_loop().time()
        raw = await self._exchange.client.create_order(
            symbol=signal.symbol,
            type="market",
            side=side.value,
            amount=float(qty),
        )
        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

        entry_price = Decimal(str(raw.get("average") or raw.get("price") or 0))
        pos = LivePosition(
            symbol=signal.symbol,
            side=signal.side.value,
            qty=qty,
            entry_price=entry_price,
            leverage=leverage,
            strategy_id=signal.strategy_id,
            metadata=signal.metadata,
        )
        self._positions.open(pos)

        await self._event_bus.publish(Event(
            type=EventType.POSITION_OPENED,
            data={"symbol": signal.symbol, "strategy": signal.strategy_id,
                   "side": signal.side.value, "entry": str(entry_price)},
            source="executor",
        ))

        await self._notify(
            f"FUNDING ENTRY: {signal.side.value.upper()} {signal.symbol}\n"
            f"Rate: {funding_bps:.1f}bps, Lev: {leverage}x\n"
            f"Entry: {entry_price}, Latency: {latency_ms:.0f}ms"
        )

        # Schedule exit: wait for funding credited event, then close
        # Bybit sends execType=Funding via private WS when funding is applied
        funding_event = asyncio.Event()
        raw_symbol = signal.symbol.replace("/", "").replace(":USDT", "")
        self._funding_events[raw_symbol] = funding_event

        task = asyncio.create_task(
            self._funding_exit(signal.symbol, raw_symbol, qty, side, funding_event, exit_delay)
        )
        self._pending_exits[signal.symbol] = task

    async def _funding_exit(
        self, symbol: str, raw_symbol: str, qty: Decimal,
        entry_side: OrderSide, funding_event: asyncio.Event, timeout: int,
    ) -> None:
        """Wait for funding credited event (or timeout), then close position."""
        logger.info("exec_funding_exit_waiting", symbol=symbol, timeout_s=timeout)

        # Wait for execType=Funding WS event, with timeout as safety net
        try:
            await asyncio.wait_for(funding_event.wait(), timeout=timeout)
            logger.info("exec_funding_credited", symbol=symbol)
        except asyncio.TimeoutError:
            logger.warning("exec_funding_exit_timeout", symbol=symbol)

        logger.info("exec_funding_exit_executing", symbol=symbol)
        close_side = "sell" if entry_side == OrderSide.BUY else "buy"
        try:
            t0 = asyncio.get_event_loop().time()
            # Use raw ccxt client to avoid _parse_order Enum issues
            raw = await self._exchange.client.create_order(
                symbol=symbol,
                type="market",
                side=close_side,
                amount=float(qty),
                params={"reduceOnly": True},
            )
            latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

            exit_price = raw.get("average") or raw.get("price") or "?"
            pos = self._positions.close(symbol)

            await self._event_bus.publish(Event(
                type=EventType.POSITION_CLOSED,
                data={"symbol": symbol, "exit": str(exit_price)},
                source="executor",
            ))

            await self._notify(
                f"FUNDING EXIT: {symbol}\n"
                f"Exit: {exit_price}, Latency: {latency_ms:.0f}ms"
            )
        except Exception:
            logger.exception("exec_funding_exit_failed", symbol=symbol)
            await self._notify(f"FUNDING EXIT FAILED: {symbol} — close manually!")
        finally:
            self._pending_exits.pop(symbol, None)
            self._funding_events.pop(raw_symbol, None)

    # ------------------------------------------------------------------
    # Event-driven execution (Miro-type: entry + TP/SL)
    # ------------------------------------------------------------------

    async def _execute_event_driven(self, signal: TradeSignal) -> None:
        side = OrderSide.BUY if signal.side == Side.LONG else OrderSide.SELL
        close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

        qty = await self._compute_min_qty(signal.symbol)

        # Market entry
        t0 = asyncio.get_event_loop().time()
        order = await self._exchange.create_order(
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            amount=qty,
        )
        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000
        entry_price = order.average or order.price or Decimal("0")

        # Place TP and SL as conditional orders
        if signal.tp:
            await self._exchange.create_order(
                symbol=signal.symbol,
                side=close_side,
                order_type=OrderType.TAKE_PROFIT,
                amount=qty,
                stop_price=Decimal(str(signal.tp)),
                reduceOnly=True,
            )
        if signal.sl:
            await self._exchange.create_order(
                symbol=signal.symbol,
                side=close_side,
                order_type=OrderType.STOP_LOSS,
                amount=qty,
                stop_price=Decimal(str(signal.sl)),
                reduceOnly=True,
            )

        pos = LivePosition(
            symbol=signal.symbol,
            side=signal.side.value,
            qty=qty,
            entry_price=entry_price,
            strategy_id=signal.strategy_id,
            metadata=signal.metadata,
        )
        self._positions.open(pos)

        await self._event_bus.publish(Event(
            type=EventType.POSITION_OPENED,
            data={"symbol": signal.symbol, "strategy": signal.strategy_id,
                   "side": signal.side.value, "entry": str(entry_price)},
            source="executor",
        ))

        await self._notify(
            f"ENTRY: {signal.side.value.upper()} {signal.symbol}\n"
            f"Entry: {entry_price}, TP: {signal.tp}, SL: {signal.sl}\n"
            f"Confidence: {signal.confidence:.2f}, Latency: {latency_ms:.0f}ms"
        )

    # ------------------------------------------------------------------
    # Rebalance (systematic strategies like Volume Ranking)
    # ------------------------------------------------------------------

    async def execute_rebalance(self, strategy_id: str, targets: list[TargetPosition]) -> None:
        """Compute diff from current positions and rebalance."""
        current = self._positions.get_by_strategy(strategy_id)
        current_map = {p.symbol: p for p in current}
        target_map = {t.symbol: t for t in targets}

        # Close positions not in targets
        for sym, pos in current_map.items():
            if sym not in target_map:
                close_side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY
                await self._exchange.create_order(
                    symbol=sym,
                    side=close_side,
                    order_type=OrderType.MARKET,
                    amount=pos.qty,
                    reduceOnly=True,
                )
                self._positions.close(sym)
                logger.info("rebalance_closed", symbol=sym, strategy=strategy_id)

        # Open new positions
        for sym, target in target_map.items():
            if sym not in current_map:
                side = OrderSide.BUY if target.side == Side.LONG else OrderSide.SELL
                qty = await self._compute_min_qty(sym)
                await self._exchange.create_order(
                    symbol=sym,
                    side=side,
                    order_type=OrderType.MARKET,
                    amount=qty,
                )
                self._positions.open(LivePosition(
                    symbol=sym,
                    side=target.side.value,
                    qty=qty,
                    entry_price=Decimal("0"),  # will be synced
                    strategy_id=strategy_id,
                ))
                logger.info("rebalance_opened", symbol=sym, side=target.side.value,
                            strategy=strategy_id)

        await self._notify(f"REBALANCE {strategy_id}: {len(targets)} targets")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _compute_min_qty(self, symbol: str) -> Decimal:
        """Get minimum order quantity for a symbol."""
        market = self._exchange.client.market(symbol)
        min_qty = market.get("limits", {}).get("amount", {}).get("min", 1)
        return Decimal(str(min_qty))

    async def _on_order_update(self, event: Event) -> None:
        """Handle WS execution/order/position updates."""
        data = event.data
        topic = data.get("_topic", "")

        if topic == "execution":
            exec_type = data.get("execType", "")
            sym = data.get("symbol", "")

            # Funding credited/debited — trigger exit for funding capture
            if exec_type == "Funding" and sym in self._funding_events:
                logger.info("ws_funding_credited", symbol=sym,
                            execFee=data.get("execFee"),
                            execQty=data.get("execQty"))
                self._funding_events[sym].set()

        elif topic == "position":
            sym = data.get("symbol", "")
            pos = self._positions.get(sym)
            if pos:
                upnl = data.get("unrealisedPnl", "0")
                pos.unrealized_pnl = Decimal(str(upnl))

        elif topic == "order":
            status = data.get("orderStatus", "")
            sym = data.get("symbol", "")
            side = data.get("side", "")
            if status == "Filled" and data.get("reduceOnly"):
                # TP or SL hit — position closed by exchange
                pos = self._positions.close(sym)
                if pos:
                    await self._event_bus.publish(Event(
                        type=EventType.POSITION_CLOSED,
                        data={"symbol": sym, "reason": "tp_sl_hit",
                               "strategy": pos.strategy_id},
                        source="executor",
                    ))
                    await self._notify(
                        f"TP/SL HIT: {sym} ({pos.strategy_id})\n"
                        f"Side: {pos.side}, Entry: {pos.entry_price}"
                    )

    async def sync_positions(self) -> None:
        """Sync position tracker with exchange reality."""
        try:
            raw = await self._exchange.fetch_positions()
            self._positions.sync_from_exchange(raw)
            logger.info("positions_synced", count=self._positions.count)
        except Exception:
            logger.exception("position_sync_failed")

    async def cancel_pending_exits(self) -> None:
        """Cancel all pending funding exit tasks (for shutdown)."""
        for sym, task in self._pending_exits.items():
            task.cancel()
        self._pending_exits.clear()

    async def _notify(self, text: str) -> None:
        if self._notifier:
            try:
                await self._notifier.send_text(text)
            except Exception:
                logger.warning("notify_failed", text=text[:50])
