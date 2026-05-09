"""Binance Futures WebSocket — public (markPrice) + private (userData).

Public endpoint: wss://fstream.binancefuture.com/stream
Private endpoint: wss://fstream.binancefuture.com/ws/<listenKey>

Mark price stream includes: fundingRate, nextFundingTime, markPrice.
User data stream includes: ACCOUNT_UPDATE with FUNDING_FEE reason.

Listen key must be obtained via REST and refreshed every 30 minutes.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import aiohttp
import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from src.core.websocket.base import WebSocketFeed
from src.engine.event_bus import EventBus, Event, EventType

logger = structlog.get_logger()

BINANCE_FAPI_REST = "https://fapi.binance.com"
BINANCE_WS_PUBLIC = "wss://fstream.binancefuture.com/stream"


class BinanceWebSocket(WebSocketFeed):
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
        self._listen_key: Optional[str] = None
        self._listen_key_task: Optional[asyncio.Task] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._ticker_subs: list[str] = []  # raw symbols for re-subscribe
        self._stats = {"messages_received": 0, "reconnects": 0}

    # ------------------------------------------------------------------
    # Listen key management
    # ------------------------------------------------------------------

    async def _create_listen_key(self) -> str:
        """POST /fapi/v1/listenKey — returns a new listen key."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BINANCE_FAPI_REST}/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": self._api_key},
            ) as resp:
                data = await resp.json()
                if "listenKey" not in data:
                    raise ConnectionError(f"Binance listen key failed: {data}")
                return data["listenKey"]

    async def _keepalive_listen_key(self) -> None:
        """PUT /fapi/v1/listenKey every 30 min to prevent expiry (60 min)."""
        while self._running:
            await asyncio.sleep(30 * 60)
            if not self._listen_key:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.put(
                        f"{BINANCE_FAPI_REST}/fapi/v1/listenKey",
                        headers={"X-MBX-APIKEY": self._api_key},
                    ) as resp:
                        if resp.status == 200:
                            logger.info("binance_ws_listen_key_refreshed")
                        else:
                            logger.warning("binance_ws_listen_key_refresh_failed",
                                           status=resp.status)
            except Exception:
                logger.exception("binance_ws_listen_key_keepalive_error")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self._connect_public()
        if self._api_key and self._api_secret:
            await self._connect_private()
        logger.info("binance_ws_connected")

    async def _connect_public(self) -> None:
        self._public_ws = await websockets.connect(
            BINANCE_WS_PUBLIC,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )

    async def _connect_private(self) -> None:
        self._listen_key = await self._create_listen_key()
        ws_url = f"wss://fstream.binancefuture.com/ws/{self._listen_key}"
        self._private_ws = await websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        # Start keepalive loop
        if self._listen_key_task:
            self._listen_key_task.cancel()
        self._listen_key_task = asyncio.create_task(self._keepalive_listen_key())
        logger.info("binance_ws_private_connected")

    async def disconnect(self) -> None:
        self._running = False
        if self._listen_key_task:
            self._listen_key_task.cancel()
            self._listen_key_task = None
        for ws in (self._public_ws, self._private_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        self._public_ws = None
        self._private_ws = None
        logger.info("binance_ws_disconnected", stats=self._stats)

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
        """Subscribe to @markPrice streams for funding rate data.

        Symbols in ccxt format (BTC/USDT:USDT) or raw (BTCUSDT).
        Binance @markPrice@1s emits: markPrice, fundingRate, nextFundingTime.
        """
        existing = set(self._ticker_subs)
        params = []
        for s in symbols:
            raw = s.replace("/", "").replace(":USDT", "").lower()
            stream = f"{raw}@markPrice@1s"
            params.append(stream)
            existing.add(raw.upper())
        self._ticker_subs = list(existing)

        if self._public_ws and params:
            msg = {"method": "SUBSCRIBE", "params": params, "id": int(asyncio.get_event_loop().time())}
            await self._public_ws.send(json.dumps(msg))
            logger.info("binance_ws_subscribed_tickers", count=len(params))

    async def subscribe_executions(self) -> None:
        """Private stream is auto-subscribed via listenKey — no explicit sub needed."""
        logger.info("binance_ws_private_auto_subscribed")

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
        consecutive_timeouts = 0
        # Private WS is silent until account activity (trades, funding);
        # public sends @markPrice every 1s so silence there is a real problem.
        max_timeouts = 6 if label == "public" else 60
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                self._stats["messages_received"] += 1
                consecutive_timeouts = 0
                data = json.loads(raw)
                await self._handle_message(data, label)
            except asyncio.TimeoutError:
                consecutive_timeouts += 1
                if consecutive_timeouts >= max_timeouts:
                    logger.error("binance_ws_dead", label=label,
                                 timeouts=consecutive_timeouts)
                    await self._reconnect(label)
                    ws = self._public_ws if label == "public" else self._private_ws
                    consecutive_timeouts = 0
            except websockets.ConnectionClosed:
                logger.warning("binance_ws_closed", label=label)
                if self._running:
                    await self._reconnect(label)
                    ws = self._public_ws if label == "public" else self._private_ws
                    consecutive_timeouts = 0
            except Exception:
                logger.exception("binance_ws_recv_error", label=label)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, data: dict, label: str) -> None:
        # Subscription confirmation
        if "result" in data and "id" in data:
            return

        # Combined stream format: {"stream": "...", "data": {"e": ...}}
        if "stream" in data and "data" in data:
            data = data["data"]

        event_type = data.get("e")

        # --- Public: markPriceUpdate ---
        # {e: "markPriceUpdate", s: "BTCUSDT", p: "...", r: "0.00010000",
        #  T: nextFundingTime_ms, ...}
        if event_type == "markPriceUpdate":
            symbol_raw = data.get("s", "")
            funding_rate_str = data.get("r", "")
            next_funding_time = data.get("T", 0)
            mark_price = data.get("p", "0")

            # Translate to Bybit-compatible format for the strategy layer
            translated = {
                "_symbol_raw": symbol_raw,
                "_source": "binance",
                "lastPrice": mark_price,
                "markPrice": mark_price,
                "fundingRate": funding_rate_str,
                "nextFundingTime": str(next_funding_time),
            }

            await self._event_bus.publish(Event(
                type=EventType.PRICE_TICK,
                data=translated,
                source="binance_ws",
            ))

            if funding_rate_str:
                await self._event_bus.publish(Event(
                    type=EventType.FUNDING_RATE,
                    data=translated,
                    source="binance_ws",
                ))

        # --- Private: ACCOUNT_UPDATE ---
        # {e: "ACCOUNT_UPDATE", T: timestamp, a: {m: "FUNDING_FEE",
        #   B: [...balances...], P: [{s: "BTCUSDT", ...}]}}
        elif event_type == "ACCOUNT_UPDATE":
            account = data.get("a", {})
            reason = account.get("m", "")

            if reason == "FUNDING_FEE":
                positions = account.get("P", [])
                for pos in positions:
                    symbol = pos.get("s", "")
                    # Emit as ORDER_UPDATE with Bybit-compatible format
                    translated = {
                        "_topic": "execution",
                        "_source": "binance",
                        "execType": "Funding",
                        "symbol": symbol,
                        "execFee": pos.get("cr", "0"),  # cumulative realized PnL
                        "execQty": pos.get("pa", "0"),  # position amount
                    }
                    await self._event_bus.publish(Event(
                        type=EventType.ORDER_UPDATE,
                        data=translated,
                        source="binance_ws",
                    ))
                    logger.info("binance_ws_funding_credited",
                                symbol=symbol,
                                realized=pos.get("cr"))

        # --- Private: ORDER_TRADE_UPDATE ---
        # {e: "ORDER_TRADE_UPDATE", o: {s: "BTCUSDT", X: "FILLED", ...}}
        elif event_type == "ORDER_TRADE_UPDATE":
            order = data.get("o", {})
            symbol = order.get("s", "")
            translated = {
                "_topic": "order",
                "_source": "binance",
                "symbol": symbol,
                "orderId": order.get("i"),
                "orderStatus": order.get("X", ""),  # NEW, FILLED, CANCELED, etc.
                "side": order.get("S", ""),
                "avgPrice": order.get("ap", "0"),
                "filledQty": order.get("z", "0"),
                "execType": order.get("x", ""),  # NEW, TRADE, etc.
            }
            await self._event_bus.publish(Event(
                type=EventType.ORDER_UPDATE,
                data=translated,
                source="binance_ws",
            ))

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect(self, label: str) -> None:
        delay = self._reconnect_delay
        while self._running:
            logger.info("binance_ws_reconnecting", label=label, delay_s=delay)
            await asyncio.sleep(delay)
            try:
                if label == "public":
                    await self._connect_public()
                    if self._ticker_subs:
                        # Re-subscribe all tickers
                        streams = [f"{s.lower()}@markPrice@1s" for s in self._ticker_subs]
                        msg = {"method": "SUBSCRIBE", "params": streams,
                               "id": int(asyncio.get_event_loop().time())}
                        await self._public_ws.send(json.dumps(msg))
                else:
                    await self._connect_private()

                self._reconnect_delay = 1.0
                self._stats["reconnects"] += 1
                logger.info("binance_ws_reconnected", label=label)
                return
            except Exception:
                delay = min(delay * 2, self._max_reconnect_delay)
                logger.warning("binance_ws_reconnect_failed", label=label, next_delay=delay)
