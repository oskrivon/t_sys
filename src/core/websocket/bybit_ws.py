"""Bybit V5 WebSocket — public (tickers) + private (executions).

Public endpoint: wss://stream.bybit.com/v5/public/linear
Private endpoint: wss://stream.bybit.com/v5/private

Ticker stream includes: lastPrice, bid1Price, ask1Price, fundingRate, nextFundingTime.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Optional

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from src.core.websocket.base import WebSocketFeed
from src.engine.event_bus import EventBus, Event, EventType

logger = structlog.get_logger()

BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
BYBIT_WS_PRIVATE = "wss://stream.bybit.com/v5/private"


class BybitWebSocket(WebSocketFeed):
    def __init__(
        self,
        event_bus: EventBus,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ) -> None:
        self._event_bus = event_bus
        self._api_key = api_key
        self._api_secret = api_secret
        self._public_ws: Optional[ClientConnection] = None
        self._private_ws: Optional[ClientConnection] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._ticker_subs: list[str] = []  # for re-subscribe on reconnect
        self._ticker_cache: dict[str, dict] = {}  # merge snapshots + deltas
        self._stats = {"messages_received": 0, "reconnects": 0}

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self._connect_public()
        if self._api_key and self._api_secret:
            await self._connect_private()
        logger.info("bybit_ws_connected")

    async def _connect_public(self) -> None:
        self._public_ws = await websockets.connect(
            BYBIT_WS_PUBLIC,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )

    async def _connect_private(self) -> None:
        self._private_ws = await websockets.connect(
            BYBIT_WS_PRIVATE,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        # Authenticate
        expires = int(time.time() * 1000) + 10_000
        sign_str = f"GET/realtime{expires}"
        signature = hmac.new(
            self._api_secret.encode(),
            sign_str.encode(),
            hashlib.sha256,
        ).hexdigest()
        auth_msg = {"op": "auth", "args": [self._api_key, expires, signature]}
        await self._private_ws.send(json.dumps(auth_msg))
        # Wait for auth response
        resp = await asyncio.wait_for(self._private_ws.recv(), timeout=5)
        data = json.loads(resp)
        if data.get("success"):
            logger.info("bybit_ws_auth_ok")
        else:
            logger.error("bybit_ws_auth_failed", response=data)
            raise ConnectionError(f"Bybit WS auth failed: {data}")

    async def disconnect(self) -> None:
        self._running = False
        for ws in (self._public_ws, self._private_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        self._public_ws = None
        self._private_ws = None
        logger.info("bybit_ws_disconnected", stats=self._stats)

    @property
    def is_connected(self) -> bool:
        pub_ok = self._public_ws is not None and self._public_ws.state.name == "OPEN"
        if self._api_key:
            priv_ok = self._private_ws is not None and self._private_ws.state.name == "OPEN"
            return pub_ok and priv_ok
        return pub_ok

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe_tickers(self, symbols: list[str]) -> None:
        """Subscribe to tickers. Symbols in ccxt format (BTC/USDT:USDT) or raw (BTCUSDT)."""
        args = []
        existing = set(self._ticker_subs)
        for s in symbols:
            raw = s.replace("/", "").replace(":USDT", "")
            topic = f"tickers.{raw}"
            args.append(topic)
            existing.add(topic)
        # Keep full list for reconnect (accumulate, don't replace)
        self._ticker_subs = list(existing)
        if self._public_ws:
            await self._public_ws.send(json.dumps({"op": "subscribe", "args": args}))
            logger.info("bybit_ws_subscribed_tickers", count=len(args))

    async def subscribe_executions(self) -> None:
        if self._private_ws:
            await self._private_ws.send(json.dumps({
                "op": "subscribe",
                "args": ["execution", "order", "position"],
            }))
            logger.info("bybit_ws_subscribed_private")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run receive loops for both WS connections."""
        self._running = True
        tasks = [self._recv_loop(self._public_ws, "public")]
        if self._private_ws:
            tasks.append(self._recv_loop(self._private_ws, "private"))
        await asyncio.gather(*tasks)

    async def _recv_loop(self, ws: ClientConnection, label: str) -> None:
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                self._stats["messages_received"] += 1
                data = json.loads(raw)
                await self._handle_message(data, label)
            except asyncio.TimeoutError:
                # No message in 30s — send ping to keep alive
                try:
                    await ws.send(json.dumps({"op": "ping"}))
                except Exception:
                    await self._reconnect(label)
                    ws = self._public_ws if label == "public" else self._private_ws
            except websockets.ConnectionClosed:
                logger.warning("bybit_ws_closed", label=label)
                if self._running:
                    await self._reconnect(label)
                    ws = self._public_ws if label == "public" else self._private_ws
            except Exception:
                logger.exception("bybit_ws_recv_error", label=label)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, data: dict, label: str) -> None:
        # Heartbeat / subscription confirmation
        op = data.get("op")
        if op in ("pong", "subscribe", "auth"):
            return

        topic = data.get("topic", "")
        msg_data = data.get("data")
        if not msg_data:
            return

        if topic.startswith("tickers."):
            symbol_raw = topic.split(".", 1)[1]
            delta = msg_data if isinstance(msg_data, dict) else msg_data[0]

            # Merge delta into cached snapshot (Bybit sends full snapshot first,
            # then deltas with only changed fields)
            cached = self._ticker_cache.get(symbol_raw, {})
            cached.update({k: v for k, v in delta.items() if v})
            cached["_symbol_raw"] = symbol_raw
            self._ticker_cache[symbol_raw] = cached

            await self._event_bus.publish(Event(
                type=EventType.PRICE_TICK,
                data=cached.copy(),
                source="bybit_ws",
            ))

            # Also emit FUNDING_RATE if funding data is present in cache
            if cached.get("fundingRate"):
                await self._event_bus.publish(Event(
                    type=EventType.FUNDING_RATE,
                    data=cached.copy(),
                    source="bybit_ws",
                ))

        elif topic in ("execution", "order", "position"):
            items = msg_data if isinstance(msg_data, list) else [msg_data]
            for item in items:
                item["_topic"] = topic
                await self._event_bus.publish(Event(
                    type=EventType.ORDER_UPDATE,
                    data=item,
                    source="bybit_ws",
                ))

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect(self, label: str) -> None:
        delay = self._reconnect_delay
        while self._running:
            logger.info("bybit_ws_reconnecting", label=label, delay_s=delay)
            await asyncio.sleep(delay)
            try:
                if label == "public":
                    await self._connect_public()
                    if self._ticker_subs:
                        await self._public_ws.send(json.dumps({
                            "op": "subscribe", "args": self._ticker_subs,
                        }))
                else:
                    await self._connect_private()
                    await self.subscribe_executions()

                self._reconnect_delay = 1.0
                self._stats["reconnects"] += 1
                logger.info("bybit_ws_reconnected", label=label)
                return
            except Exception:
                delay = min(delay * 2, self._max_reconnect_delay)
                logger.warning("bybit_ws_reconnect_failed", label=label, next_delay=delay)
