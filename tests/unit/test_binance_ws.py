"""Tests for BinanceWebSocket message handling and subscriptions.

Verifies that Binance WS messages are correctly translated to the
internal EventBus format, compatible with the existing strategy
and executor layers (which expect Bybit-style fields).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.websocket.binance_ws import BinanceWebSocket
from src.engine.event_bus import EventBus, Event, EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def ws(bus):
    return BinanceWebSocket(bus, api_key="test_key", api_secret="test_secret")


def _collected_events(bus: EventBus, event_type: EventType) -> list[Event]:
    """Collect events published to bus by running the handler."""
    collected = []
    original = bus.publish

    async def spy(event: Event):
        collected.append(event)
        await original(event)

    bus.publish = spy
    return collected


# ---------------------------------------------------------------------------
# markPriceUpdate (public) -> PRICE_TICK + FUNDING_RATE
# ---------------------------------------------------------------------------

class TestMarkPriceUpdate:
    """Public @markPrice stream messages."""

    async def test_emits_price_tick(self, ws, bus):
        events = _collected_events(bus, EventType.PRICE_TICK)

        msg = {
            "e": "markPriceUpdate",
            "s": "BTCUSDT",
            "p": "67000.50",
            "r": "0.00010000",
            "T": 1700000000000,
        }
        await ws._handle_message(msg, "public")

        price_ticks = [e for e in events if e.type == EventType.PRICE_TICK]
        assert len(price_ticks) == 1
        data = price_ticks[0].data
        assert data["_symbol_raw"] == "BTCUSDT"
        assert data["lastPrice"] == "67000.50"
        assert data["fundingRate"] == "0.00010000"
        assert data["nextFundingTime"] == "1700000000000"
        assert data["_source"] == "binance"

    async def test_emits_funding_rate_event(self, ws, bus):
        events = _collected_events(bus, EventType.FUNDING_RATE)

        msg = {
            "e": "markPriceUpdate",
            "s": "ETHUSDT",
            "p": "3500.00",
            "r": "-0.00050000",
            "T": 1700000000000,
        }
        await ws._handle_message(msg, "public")

        funding_events = [e for e in events if e.type == EventType.FUNDING_RATE]
        assert len(funding_events) == 1
        assert funding_events[0].data["fundingRate"] == "-0.00050000"
        assert funding_events[0].source == "binance_ws"

    async def test_no_funding_event_when_rate_empty(self, ws, bus):
        events = _collected_events(bus, EventType.FUNDING_RATE)

        msg = {
            "e": "markPriceUpdate",
            "s": "BTCUSDT",
            "p": "67000.00",
            "r": "",
            "T": 0,
        }
        await ws._handle_message(msg, "public")

        funding_events = [e for e in events if e.type == EventType.FUNDING_RATE]
        assert len(funding_events) == 0

    async def test_source_is_binance_ws(self, ws, bus):
        events = _collected_events(bus, EventType.PRICE_TICK)

        msg = {"e": "markPriceUpdate", "s": "SOLUSDT", "p": "150.0", "r": "0.0001", "T": 0}
        await ws._handle_message(msg, "public")

        assert events[0].source == "binance_ws"


# ---------------------------------------------------------------------------
# ACCOUNT_UPDATE with FUNDING_FEE (private) -> ORDER_UPDATE
# ---------------------------------------------------------------------------

class TestFundingFeeEvent:
    """Private user data: ACCOUNT_UPDATE with reason FUNDING_FEE."""

    async def test_emits_order_update_with_funding_exec_type(self, ws, bus):
        events = _collected_events(bus, EventType.ORDER_UPDATE)

        msg = {
            "e": "ACCOUNT_UPDATE",
            "T": 1700000000000,
            "a": {
                "m": "FUNDING_FEE",
                "B": [{"a": "USDT", "wb": "1000.00"}],
                "P": [{"s": "BTCUSDT", "pa": "0.001", "cr": "0.50"}],
            },
        }
        await ws._handle_message(msg, "private")

        order_events = [e for e in events if e.type == EventType.ORDER_UPDATE]
        assert len(order_events) == 1
        data = order_events[0].data
        # Must be compatible with executor's execType check
        assert data["execType"] == "Funding"
        assert data["symbol"] == "BTCUSDT"
        assert data["_topic"] == "execution"

    async def test_multiple_positions_in_funding_event(self, ws, bus):
        events = _collected_events(bus, EventType.ORDER_UPDATE)

        msg = {
            "e": "ACCOUNT_UPDATE",
            "T": 1700000000000,
            "a": {
                "m": "FUNDING_FEE",
                "B": [],
                "P": [
                    {"s": "BTCUSDT", "pa": "0.001", "cr": "0.50"},
                    {"s": "ETHUSDT", "pa": "0.01", "cr": "0.30"},
                ],
            },
        }
        await ws._handle_message(msg, "private")

        order_events = [e for e in events if e.type == EventType.ORDER_UPDATE]
        assert len(order_events) == 2
        symbols = {e.data["symbol"] for e in order_events}
        assert symbols == {"BTCUSDT", "ETHUSDT"}

    async def test_non_funding_account_update_ignored(self, ws, bus):
        events = _collected_events(bus, EventType.ORDER_UPDATE)

        msg = {
            "e": "ACCOUNT_UPDATE",
            "T": 1700000000000,
            "a": {
                "m": "ORDER",  # not FUNDING_FEE
                "B": [],
                "P": [{"s": "BTCUSDT", "pa": "0.001", "cr": "0"}],
            },
        }
        await ws._handle_message(msg, "private")

        # No funding-type events should be emitted
        funding_events = [e for e in events if e.data.get("execType") == "Funding"]
        assert len(funding_events) == 0


# ---------------------------------------------------------------------------
# ORDER_TRADE_UPDATE (private) -> ORDER_UPDATE
# ---------------------------------------------------------------------------

class TestOrderTradeUpdate:
    """Private user data: ORDER_TRADE_UPDATE for fills."""

    async def test_emits_order_update(self, ws, bus):
        events = _collected_events(bus, EventType.ORDER_UPDATE)

        msg = {
            "e": "ORDER_TRADE_UPDATE",
            "T": 1700000000000,
            "o": {
                "s": "ETHUSDT",
                "i": 12345,
                "X": "FILLED",
                "S": "BUY",
                "ap": "3500.00",
                "z": "0.01",
                "x": "TRADE",
            },
        }
        await ws._handle_message(msg, "private")

        order_events = [e for e in events if e.type == EventType.ORDER_UPDATE]
        assert len(order_events) == 1
        data = order_events[0].data
        assert data["symbol"] == "ETHUSDT"
        assert data["orderStatus"] == "FILLED"
        assert data["avgPrice"] == "3500.00"
        assert data["_topic"] == "order"


# ---------------------------------------------------------------------------
# Subscription confirmation — should be silently ignored
# ---------------------------------------------------------------------------

class TestSubscriptionConfirmation:
    async def test_confirmation_ignored(self, ws, bus):
        events = _collected_events(bus, EventType.PRICE_TICK)

        msg = {"result": None, "id": 1}
        await ws._handle_message(msg, "public")

        assert len(events) == 0


# ---------------------------------------------------------------------------
# subscribe_tickers — correct stream names
# ---------------------------------------------------------------------------

class TestSubscribeTickers:
    async def test_builds_correct_stream_names(self, ws):
        mock_ws = AsyncMock()
        ws._public_ws = mock_ws

        await ws.subscribe_tickers(["BTC/USDT:USDT", "ETH/USDT:USDT"])

        mock_ws.send.assert_called_once()
        sent = __import__("json").loads(mock_ws.send.call_args[0][0])
        assert sent["method"] == "SUBSCRIBE"
        assert "btcusdt@markPrice@1s" in sent["params"]
        assert "ethusdt@markPrice@1s" in sent["params"]

    async def test_accumulates_subscriptions(self, ws):
        mock_ws = AsyncMock()
        ws._public_ws = mock_ws

        await ws.subscribe_tickers(["BTC/USDT:USDT"])
        await ws.subscribe_tickers(["ETH/USDT:USDT"])

        assert "BTCUSDT" in ws._ticker_subs
        assert "ETHUSDT" in ws._ticker_subs

    async def test_handles_raw_symbol_format(self, ws):
        mock_ws = AsyncMock()
        ws._public_ws = mock_ws

        await ws.subscribe_tickers(["SOLUSDT"])

        sent = __import__("json").loads(mock_ws.send.call_args[0][0])
        assert "solusdt@markPrice@1s" in sent["params"]


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------

class TestConnectionState:
    def test_not_connected_initially(self, ws):
        assert not ws.is_connected

    def test_connected_public_only(self, ws):
        ws._api_key = None
        mock_ws = MagicMock()
        mock_ws.state.name = "OPEN"
        ws._public_ws = mock_ws
        assert ws.is_connected

    def test_needs_both_when_private(self, ws):
        mock_pub = MagicMock()
        mock_pub.state.name = "OPEN"
        mock_priv = MagicMock()
        mock_priv.state.name = "OPEN"
        ws._public_ws = mock_pub
        ws._private_ws = mock_priv
        assert ws.is_connected

    def test_not_connected_if_private_missing(self, ws):
        mock_pub = MagicMock()
        mock_pub.state.name = "OPEN"
        ws._public_ws = mock_pub
        ws._private_ws = None
        assert not ws.is_connected
