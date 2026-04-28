"""Tests for BybitWebSocket reconnection — ws ref must update after reconnect.

Regression: after WS disconnect, _recv_loop kept reading from the OLD closed
connection instead of the new one, causing an infinite reconnect loop where
no messages were actually processed.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import websockets

from src.core.websocket.bybit_ws import BybitWebSocket
from src.engine.event_bus import EventBus


@pytest.fixture
def event_bus():
    return EventBus()


def _make_mock_ws(messages: list[str] | None = None):
    """Create a mock WebSocket connection.

    If messages is provided, recv() yields them in order then blocks forever.
    """
    ws = AsyncMock()
    state = MagicMock()
    state.name = "OPEN"
    ws.state = state
    if messages is not None:
        it = iter(messages)

        async def _recv():
            try:
                return next(it)
            except StopIteration:
                await asyncio.sleep(999)  # block forever

        ws.recv = _recv
    return ws


class TestRecvLoopUpdatesWsRef:
    """After reconnect, _recv_loop must read from the NEW connection."""

    async def test_public_ws_ref_updated_after_connection_closed(self, event_bus):
        bws = BybitWebSocket(event_bus)
        bws._running = True

        old_ws = _make_mock_ws()
        # First recv() raises ConnectionClosed to trigger reconnect
        old_ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(None, None)
        )

        # New WS that returns a valid ticker message then blocks
        ticker_msg = json.dumps({
            "topic": "tickers.BTCUSDT",
            "data": {"fundingRate": "0.001", "lastPrice": "50000",
                     "nextFundingTime": "1700000000000",
                     "_symbol_raw": "BTCUSDT"},
        })
        new_ws = _make_mock_ws([ticker_msg])

        reconnect_called = asyncio.Event()

        async def mock_reconnect(label):
            bws._public_ws = new_ws
            reconnect_called.set()

        bws._reconnect = mock_reconnect

        # Run recv loop in background
        task = asyncio.create_task(bws._recv_loop(old_ws, "public"))

        # Wait for reconnect to happen
        await asyncio.wait_for(reconnect_called.wait(), timeout=2.0)
        # Give time for the loop to read from new_ws
        await asyncio.sleep(0.1)

        bws._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The message from new_ws should have been processed
        assert bws._stats["messages_received"] >= 1

    async def test_private_ws_ref_updated_after_connection_closed(self, event_bus):
        bws = BybitWebSocket(event_bus, api_key="k", api_secret="s")
        bws._running = True

        old_ws = _make_mock_ws()
        old_ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(None, None)
        )

        exec_msg = json.dumps({
            "topic": "execution",
            "data": [{"execType": "Funding", "symbol": "BTCUSDT",
                       "execFee": "-0.001", "execQty": "1"}],
        })
        new_ws = _make_mock_ws([exec_msg])

        async def mock_reconnect(label):
            bws._private_ws = new_ws

        bws._reconnect = mock_reconnect

        task = asyncio.create_task(bws._recv_loop(old_ws, "private"))
        await asyncio.sleep(0.2)

        bws._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert bws._stats["messages_received"] >= 1

    async def test_ws_ref_updated_after_ping_failure(self, event_bus):
        """When ping fails (send raises), reconnect + update ws ref."""
        bws = BybitWebSocket(event_bus)
        bws._running = True

        old_ws = _make_mock_ws()
        call_count = 0

        async def recv_timeout_then_block():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            await asyncio.sleep(999)

        old_ws.recv = recv_timeout_then_block
        # Ping fails → triggers reconnect path
        old_ws.send = AsyncMock(side_effect=Exception("send failed"))

        ticker_msg = json.dumps({
            "topic": "tickers.ETHUSDT",
            "data": {"lastPrice": "3000"},
        })
        new_ws = _make_mock_ws([ticker_msg])

        async def mock_reconnect(label):
            bws._public_ws = new_ws

        bws._reconnect = mock_reconnect

        task = asyncio.create_task(bws._recv_loop(old_ws, "public"))
        await asyncio.sleep(0.2)

        bws._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert bws._stats["messages_received"] >= 1


class TestReconnectDoesNotLoop:
    """Verify that reconnect doesn't create an infinite loop on stale ref."""

    async def test_no_rapid_reconnect_loop(self, event_bus):
        """Before the fix, each reconnect would immediately hit ConnectionClosed
        on the stale ws, causing rapid-fire reconnects. After the fix, the loop
        should read from the new connection and NOT call reconnect again."""
        bws = BybitWebSocket(event_bus)
        bws._running = True

        old_ws = _make_mock_ws()
        old_ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(None, None)
        )

        reconnect_count = 0
        ticker_msg = json.dumps({
            "topic": "tickers.BTCUSDT",
            "data": {"lastPrice": "50000"},
        })

        async def mock_reconnect(label):
            nonlocal reconnect_count
            reconnect_count += 1
            # Each reconnect creates a "new" working connection
            bws._public_ws = _make_mock_ws([ticker_msg])

        bws._reconnect = mock_reconnect

        task = asyncio.create_task(bws._recv_loop(old_ws, "public"))
        await asyncio.sleep(0.3)

        bws._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should reconnect exactly once, not loop
        assert reconnect_count == 1, (
            f"Expected 1 reconnect, got {reconnect_count} — "
            f"stale ws ref is causing a reconnect loop"
        )
