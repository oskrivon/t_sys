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
        state=None,
        exchange_name: str = "bybit",
    ) -> None:
        self._exchange = exchange
        self._event_bus = event_bus
        self._positions = positions
        self._notifier = notifier
        self._state = state
        self._tag = f"[{exchange_name}] "
        self._pending_exits: dict[str, asyncio.Task] = {}
        self._executing: set[str] = set()  # symbols currently being executed (lock)
        self._funding_events: dict[str, asyncio.Event] = {}  # raw_symbol -> event for funding credited
        # Circuit breaker: pause trading after consecutive failures
        self._consecutive_failures: int = 0
        self._circuit_open: bool = False
        self._max_failures: int = 3
        self._circuit_cooldown: int = 300  # 5 min

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

        # Rebalance events: log targets for paper tracking, no execution
        if isinstance(signal, dict) and signal.get("type") == "rebalance":
            await self._log_rebalance(signal)
            return

        if not isinstance(signal, TradeSignal):
            return

        # Circuit breaker: skip all signals while open
        if self._circuit_open:
            logger.warning("circuit_breaker_open", symbol=signal.symbol)
            return

        sig_type = signal.metadata.get("type", "")
        is_reduce = signal.metadata.get("reduce_only", False)

        # Pairs trade exit: close existing position
        if sig_type == "pairs_trade" and is_reduce:
            try:
                await self._execute_pairs_exit(signal)
                return
            except Exception:
                logger.exception("exec_pairs_exit_failed", symbol=signal.symbol)
                return

        # Prevent duplicate execution: check both position and in-flight lock
        if self._positions.has_position(signal.symbol) or signal.symbol in self._executing:
            return

        try:
            if sig_type == "funding_capture":
                await self._execute_funding_capture(signal)
            else:
                await self._execute_event_driven(signal)
            # Success — reset failure counter
            self._consecutive_failures = 0
        except Exception:
            logger.exception("exec_order_failed",
                             symbol=signal.symbol,
                             strategy=signal.strategy_id)
            await self._notify(f"ORDER FAILED: {signal.symbol} ({signal.strategy_id})")
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                self._circuit_open = True
                logger.error("circuit_breaker_triggered",
                             failures=self._consecutive_failures)
                await self._notify(
                    f"CIRCUIT BREAKER: {self._consecutive_failures} consecutive failures, "
                    f"pausing {self._circuit_cooldown}s")
                asyncio.create_task(self._reset_circuit())

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

        side = OrderSide.BUY if signal.side == Side.LONG else OrderSide.SELL

        # Fast path: use pre-computed qty (leverage already set by strategy)
        precomputed_qty = signal.metadata.get("_precomputed_qty")
        if precomputed_qty and signal.metadata.get("_leverage_set"):
            qty = Decimal(str(precomputed_qty))
        else:
            # Slow fallback — should not happen if precompute is wired correctly
            logger.warning("precompute_missing_using_fallback",
                           symbol=signal.symbol,
                           funding_bps=funding_bps)
            await self._exchange.set_leverage(leverage, signal.symbol)
            target_notional = signal.metadata.get("target_notional", 0)
            qty = await self._compute_qty(signal.symbol, target_notional)

        logger.info("exec_funding_entry",
                     symbol=signal.symbol,
                     side=side.value,
                     qty=str(qty),
                     leverage=leverage,
                     funding_bps=funding_bps)

        # Limit order entry with market fallback (save maker fee ~3bps)
        entry_price, latency_ms = await self._funding_entry_limit(
            signal.symbol, side.value, float(qty),
            limit_wait_s=signal.metadata.get("limit_wait_seconds", 3.0),
        )
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

        if self._state:
            self._state.log_trade(
                symbol=signal.symbol, strategy_id=signal.strategy_id,
                side=signal.side.value, action="open",
                price=str(entry_price), qty=str(qty),
                metadata={"funding_bps": funding_bps, "leverage": leverage,
                           "latency_ms": round(latency_ms, 1),
                           "book_precompute": signal.metadata.get("book_precompute", {}),
                           "book_t2s": signal.metadata.get("book_t2s", {})},
            )

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

        # Compute timeout relative to settlement time, not entry time.
        # Entry is at T-5s, settlement at T-0. We want to exit at T+5s worst case.
        next_funding_ms = signal.metadata.get("next_funding_time", 0)
        if next_funding_ms:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            secs_to_settlement = max((next_funding_ms - now_ms) / 1000, 0)
            # Wait until settlement + 5s buffer for WS event
            exit_timeout = secs_to_settlement + 5
        else:
            exit_timeout = exit_delay

        task = asyncio.create_task(
            self._funding_exit(symbol=signal.symbol, raw_symbol=raw_symbol,
                               qty=qty, entry_side=side,
                               funding_event=funding_event, timeout=exit_timeout)
        )
        self._pending_exits[signal.symbol] = task

    async def _funding_exit(
        self, symbol: str, raw_symbol: str, qty: Decimal,
        entry_side: OrderSide, funding_event: asyncio.Event, timeout: float,
    ) -> None:
        """Wait for funding credited event (or timeout), then close position."""
        logger.info("exec_funding_exit_waiting", symbol=symbol, timeout_s=round(timeout, 1))

        # Wait for execType=Funding WS event, with timeout as safety net
        try:
            await asyncio.wait_for(funding_event.wait(), timeout=timeout)
            logger.info("exec_funding_credited", symbol=symbol)
        except asyncio.TimeoutError:
            # WS event didn't arrive — verify via REST that settlement happened
            logger.warning("exec_funding_exit_timeout", symbol=symbol,
                           timeout_s=round(timeout, 1))
            try:
                funded = await self._check_funding_via_rest(symbol, raw_symbol)
                if funded:
                    logger.info("exec_funding_confirmed_via_rest", symbol=symbol)
                else:
                    logger.warning("exec_funding_not_confirmed", symbol=symbol)
            except Exception:
                logger.warning("exec_funding_rest_check_failed", symbol=symbol)

        logger.info("exec_funding_exit_executing", symbol=symbol)
        close_side = "sell" if entry_side == OrderSide.BUY else "buy"
        try:
            t0 = asyncio.get_event_loop().time()
            # Try limit exit first (save ~3bps maker vs taker), fallback to market
            exit_price, latency_ms = await self._funding_exit_limit(
                symbol, close_side, float(qty), limit_wait_s=2.0,
            )
            pos = self._positions.close(symbol)

            if self._state and pos:
                entry_p = float(pos.entry_price) if pos.entry_price else 0
                exit_p = float(exit_price) if exit_price != "?" else 0
                if entry_p > 0 and exit_p > 0:
                    if pos.side == "long":
                        pnl_pct = (exit_p - entry_p) / entry_p * 100
                    else:
                        pnl_pct = (entry_p - exit_p) / entry_p * 100
                else:
                    pnl_pct = 0.0
                self._state.log_trade(
                    symbol=symbol, strategy_id=pos.strategy_id or "funding_capture",
                    side=pos.side or "?", action="close",
                    price=str(exit_price), qty=str(pos.qty),
                    pnl=f"{pnl_pct:.4f}%",
                    metadata={"entry_price": str(pos.entry_price),
                              "funding_bps": pos.metadata.get("funding_rate_bps", 0),
                              "latency_ms": round(latency_ms, 1),
                              "book_t2s": pos.metadata.get("book_t2s", {})},
                )

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

    async def _check_funding_via_rest(self, symbol: str, raw_symbol: str) -> bool:
        """Check via REST API if funding was credited (fallback when WS is silent)."""
        client = self._exchange.client
        exchange_id = getattr(client, "id", "bybit")

        if exchange_id == "binance":
            # GET /fapi/v1/income?incomeType=FUNDING_FEE&symbol=X&limit=1
            income = await client.fapiPrivateGetIncome({
                "incomeType": "FUNDING_FEE",
                "symbol": raw_symbol,
                "limit": 1,
            })
            if income:
                # Check if the latest funding is recent (within last 60s)
                ts = int(income[-1].get("time", 0))
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                if now_ms - ts < 60_000:
                    return True
        elif exchange_id == "bybit":
            # Bybit WS works fine — this is just a safety net
            return True

        return False

    # ------------------------------------------------------------------
    # Limit entry with market fallback (funding capture)
    # ------------------------------------------------------------------

    async def _funding_entry_limit(
        self, symbol: str, side: str, qty: float, limit_wait_s: float = 3.0,
    ) -> tuple[Decimal, float]:
        """Try limit order at best bid/ask, fallback to market if not filled.

        Returns (entry_price, latency_ms).
        """
        client = self._exchange.client
        t0 = asyncio.get_event_loop().time()

        # Get current best price for limit
        try:
            ob = await client.fetch_order_book(symbol, limit=5)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            if side == "buy" and bids:
                limit_price = bids[0][0]  # best bid — we join the bid
            elif side == "sell" and asks:
                limit_price = asks[0][0]  # best ask — we join the ask
            else:
                raise ValueError("empty orderbook")
        except Exception as e:
            logger.warning("limit_entry_ob_failed_using_market", symbol=symbol, error=str(e))
            raw = await client.create_order(symbol, "market", side, qty)
            latency_ms = (asyncio.get_event_loop().time() - t0) * 1000
            price = raw.get("average") or raw.get("price") or 0
            return Decimal(str(price)), latency_ms

        # Place post-only limit order
        # Bybit: "PostOnly", Binance Futures: "GTX" (Good Till Crossing)
        exchange_id = getattr(client, "id", "bybit")
        tif = "GTX" if exchange_id == "binance" else "PostOnly"
        try:
            raw = await client.create_order(
                symbol, "limit", side, qty, limit_price,
                params={"timeInForce": tif},
            )
        except Exception as e:
            # PostOnly rejected (price crossed) — immediate market fallback
            logger.warning("limit_entry_rejected_using_market", symbol=symbol, error=str(e))
            raw = await client.create_order(symbol, "market", side, qty)
            latency_ms = (asyncio.get_event_loop().time() - t0) * 1000
            price = raw.get("average") or raw.get("price") or 0
            return Decimal(str(price)), latency_ms

        order_id = raw.get("id")
        logger.info("limit_entry_placed", symbol=symbol, side=side,
                     price=limit_price, order_id=order_id)

        # Poll for fill
        filled = False
        poll_interval = 0.3
        elapsed = 0.0
        while elapsed < limit_wait_s:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                status = await client.fetch_order(order_id, symbol)
                if status.get("status") == "closed":
                    filled = True
                    raw = status
                    break
            except Exception:
                pass

        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

        if filled:
            price = raw.get("average") or raw.get("price") or limit_price
            logger.info("limit_entry_filled", symbol=symbol, price=price,
                         latency_ms=round(latency_ms, 1))
            return Decimal(str(price)), latency_ms

        # Not filled — cancel and market fallback
        try:
            await client.cancel_order(order_id, symbol)
        except Exception:
            pass  # might be partially filled or already cancelled

        # Check if partially filled
        try:
            status = await client.fetch_order(order_id, symbol)
            filled_qty = float(status.get("filled", 0))
        except Exception:
            filled_qty = 0.0

        remaining = qty - filled_qty

        if remaining > 0:
            try:
                raw_market = await client.create_order(symbol, "market", side, remaining)
                logger.info("limit_entry_fallback_market", symbol=symbol,
                             filled_limit=filled_qty, remaining=remaining)
            except Exception:
                logger.exception("limit_entry_market_fallback_failed", symbol=symbol)
                if filled_qty > 0:
                    # Partial fill — use limit price as entry
                    return Decimal(str(limit_price)), latency_ms
                raise

        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

        # Compute blended entry price
        if filled_qty > 0 and remaining > 0:
            market_price = float(raw_market.get("average") or raw_market.get("price") or limit_price)
            blended = (filled_qty * limit_price + remaining * market_price) / qty
            logger.info("limit_entry_partial_blend", symbol=symbol,
                         limit_qty=filled_qty, market_qty=remaining,
                         limit_price=limit_price, market_price=market_price,
                         blended=round(blended, 8))
            return Decimal(str(round(blended, 8))), latency_ms
        else:
            # Fully market
            price = raw_market.get("average") or raw_market.get("price") or 0
            logger.info("limit_entry_full_market_fallback", symbol=symbol,
                         price=price, latency_ms=round(latency_ms, 1))
            return Decimal(str(price)), latency_ms

    # ------------------------------------------------------------------
    # Event-driven execution (Miro-type: entry + TP/SL)
    # ------------------------------------------------------------------

    async def _funding_exit_limit(
        self, symbol: str, side: str, qty: float, limit_wait_s: float = 2.0,
    ) -> tuple[Decimal, float]:
        """Try limit exit at best bid/ask, fallback to market. Same logic as entry."""
        client = self._exchange.client
        t0 = asyncio.get_event_loop().time()

        try:
            ob = await client.fetch_order_book(symbol, limit=5)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            if side == "sell" and bids:
                limit_price = bids[0][0]
            elif side == "buy" and asks:
                limit_price = asks[0][0]
            else:
                raise ValueError("empty orderbook")
        except Exception as e:
            logger.warning("limit_exit_ob_failed_using_market", symbol=symbol, error=str(e))
            raw = await client.create_order(symbol, "market", side, qty,
                                            params={"reduceOnly": True})
            latency_ms = (asyncio.get_event_loop().time() - t0) * 1000
            price = raw.get("average") or raw.get("price") or 0
            return Decimal(str(price)), latency_ms

        exchange_id = getattr(client, "id", "bybit")
        tif = "GTX" if exchange_id == "binance" else "PostOnly"
        try:
            raw = await client.create_order(
                symbol, "limit", side, qty, limit_price,
                params={"timeInForce": tif, "reduceOnly": True},
            )
        except Exception:
            logger.warning("limit_exit_rejected_using_market", symbol=symbol)
            raw = await client.create_order(symbol, "market", side, qty,
                                            params={"reduceOnly": True})
            latency_ms = (asyncio.get_event_loop().time() - t0) * 1000
            price = raw.get("average") or raw.get("price") or 0
            return Decimal(str(price)), latency_ms

        order_id = raw.get("id")
        logger.info("limit_exit_placed", symbol=symbol, side=side, price=limit_price)

        filled = False
        elapsed = 0.0
        poll_interval = 0.3
        while elapsed < limit_wait_s:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                status = await client.fetch_order(order_id, symbol)
                if status.get("status") == "closed":
                    filled = True
                    raw = status
                    break
            except Exception:
                pass

        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

        if filled:
            price = raw.get("average") or raw.get("price") or limit_price
            logger.info("limit_exit_filled", symbol=symbol, price=price)
            return Decimal(str(price)), latency_ms

        # Cancel and market fallback
        try:
            await client.cancel_order(order_id, symbol)
        except Exception:
            pass

        try:
            raw_market = await client.create_order(symbol, "market", side, qty,
                                                   params={"reduceOnly": True})
        except Exception:
            logger.exception("limit_exit_market_fallback_failed", symbol=symbol)
            return Decimal(str(limit_price)), latency_ms

        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000
        price = raw_market.get("average") or raw_market.get("price") or limit_price
        logger.info("limit_exit_market_fallback", symbol=symbol, price=price)
        return Decimal(str(price)), latency_ms

    # ------------------------------------------------------------------
    # Event-driven execution (Miro-type: entry + TP/SL)
    # ------------------------------------------------------------------

    async def _execute_event_driven(self, signal: TradeSignal) -> None:
        side = OrderSide.BUY if signal.side == Side.LONG else OrderSide.SELL
        close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

        qty = await self._compute_qty(signal.symbol)

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

        pair = signal.metadata.get("pair", "")
        if pair:
            await self._notify(
                f"PAIRS ENTRY: {signal.side.value.upper()} {signal.symbol}\n"
                f"Pair: {pair}, Leg: {signal.metadata.get('leg', '?')}\n"
                f"Z-score: {signal.metadata.get('z_score', '?')}, Entry: {entry_price}"
            )
        else:
            await self._notify(
                f"ENTRY: {signal.side.value.upper()} {signal.symbol}\n"
                f"Entry: {entry_price}, TP: {signal.tp}, SL: {signal.sl}\n"
                f"Confidence: {signal.confidence:.2f}, Latency: {latency_ms:.0f}ms"
            )

    async def _execute_pairs_exit(self, signal: TradeSignal) -> None:
        """Close one leg of a pairs trade."""
        pos = self._positions.get(signal.symbol)
        if not pos:
            logger.warning("pairs_exit_no_position", symbol=signal.symbol)
            return

        qty = pos.qty
        close_side = "sell" if pos.side == "long" else "buy"

        t0 = asyncio.get_event_loop().time()
        raw = await self._exchange.client.create_order(
            symbol=signal.symbol,
            type="market",
            side=close_side,
            amount=float(qty),
            params={"reduceOnly": True},
        )
        latency_ms = (asyncio.get_event_loop().time() - t0) * 1000

        exit_price = raw.get("average") or raw.get("price") or 0
        entry_price = float(pos.entry_price)
        if entry_price > 0:
            if pos.side == "long":
                pnl_pct = (float(exit_price) - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - float(exit_price)) / entry_price * 100
        else:
            pnl_pct = 0

        self._positions.close(signal.symbol)

        pair = signal.metadata.get("pair", "")
        reason = signal.metadata.get("exit_reason", "")
        logger.info("pairs_exit_executed",
                     symbol=signal.symbol, pair=pair,
                     pnl_pct=round(pnl_pct, 3),
                     reason=reason, latency_ms=round(latency_ms))

        if self._state:
            self._state.log_trade(
                symbol=signal.symbol,
                strategy_id=signal.strategy_id,
                side=pos.side,
                action="close",
                price=str(exit_price),
                qty=str(qty),
                pnl=f"{pnl_pct:.4f}%",
                metadata={
                    "entry_price": str(entry_price),
                    "pair": pair,
                    "exit_reason": reason,
                    "entry_z": signal.metadata.get("entry_z"),
                    "exit_z": signal.metadata.get("exit_z"),
                    "leg": signal.metadata.get("leg"),
                    "latency_ms": round(latency_ms),
                },
            )

        await self._notify(
            f"PAIRS EXIT: {signal.symbol} ({pair})\n"
            f"PnL: {pnl_pct:+.3f}%, Reason: {reason}\n"
            f"Z: {signal.metadata.get('entry_z', '?')} -> {signal.metadata.get('exit_z', '?')}"
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
                qty = await self._compute_qty(sym)
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
    # Paper rebalance logging
    # ------------------------------------------------------------------

    async def _log_rebalance(self, signal: dict) -> None:
        """Log rebalance targets to state DB for paper tracking."""
        strategy_id = signal.get("strategy_id", "unknown")
        targets = signal.get("targets", [])
        if not targets:
            return
        for target in targets:
            self._state.log_trade(
                symbol=target.symbol,
                strategy_id=strategy_id,
                side=target.side.value,
                action="paper_target",
                metadata={"weight": target.weight, "score": target.score},
            )
        logger.info("rebalance_paper_logged",
                    strategy=strategy_id, targets=len(targets))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    TAKER_FEE: float = 0.00060  # Bybit VIP0 taker fee per side (6bps)

    @staticmethod
    def compute_volatility_adjusted_size(
        capital: float,
        risk_per_trade_pct: float,
        atr_pct: float,
        max_position_pct: float = 30.0,
    ) -> float:
        """Compute position size (USD) scaled by instrument volatility.

        Args:
            capital: Available capital for this strategy.
            risk_per_trade_pct: Max % of capital to risk per trade (e.g. 1.0).
            atr_pct: ATR(14) / close — the instrument's recent volatility
                as a fraction (e.g. 0.03 for 3%).
            max_position_pct: Hard cap on position as % of capital.

        Returns:
            Target notional in USD.
        """
        if atr_pct <= 0:
            atr_pct = 0.02  # fallback: assume 2% daily vol

        risk_usd = capital * risk_per_trade_pct / 100
        position_usd = risk_usd / atr_pct
        cap = capital * max_position_pct / 100
        return min(position_usd, cap)

    async def _compute_qty(
        self,
        symbol: str,
        target_notional: float = 0,
        atr_pct: float = 0.0,
        capital: float = 0.0,
        risk_per_trade_pct: float = 0.0,
    ) -> Decimal:
        """Compute order quantity, optionally volatility-adjusted.

        If ``atr_pct``, ``capital``, and ``risk_per_trade_pct`` are all
        provided, the target notional is computed via inverse-vol sizing.
        Otherwise falls back to the explicit ``target_notional``.
        """
        # Volatility-adjusted sizing when metadata is available
        if atr_pct > 0 and capital > 0 and risk_per_trade_pct > 0:
            target_notional = self.compute_volatility_adjusted_size(
                capital=capital,
                risk_per_trade_pct=risk_per_trade_pct,
                atr_pct=atr_pct,
            )
            logger.debug("vol_adjusted_size",
                         symbol=symbol, atr_pct=atr_pct,
                         target_notional=target_notional)

        market = self._exchange.client.market(symbol)
        min_qty = float(market.get("limits", {}).get("amount", {}).get("min", 1))
        qty_step = float(market.get("precision", {}).get("amount", min_qty))

        if target_notional > 0:
            ticker = await self._exchange.client.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0)
            if price > 0:
                # Subtract RT fees so position + fees fit within target
                effective = target_notional * (1 - self.TAKER_FEE * 2)
                raw_qty = effective / price
                if qty_step > 0:
                    raw_qty = int(raw_qty / qty_step) * qty_step
                qty = max(raw_qty, min_qty)
                return Decimal(str(qty))

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

    async def _reset_circuit(self) -> None:
        """Re-enable trading after cooldown period."""
        await asyncio.sleep(self._circuit_cooldown)
        self._circuit_open = False
        self._consecutive_failures = 0
        logger.info("circuit_breaker_reset")
        await self._notify("Circuit breaker reset — trading resumed")

    async def cancel_pending_exits(self) -> None:
        """Cancel all pending funding exit tasks (for shutdown)."""
        for sym, task in self._pending_exits.items():
            task.cancel()
        self._pending_exits.clear()

    async def _notify(self, text: str) -> None:
        if self._notifier:
            try:
                await self._notifier.send_text(self._tag + text)
            except Exception:
                logger.warning("notify_failed", text=text[:50])
