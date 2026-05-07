"""Tests for engine exchange factory functions and TradingEngine init.

Verifies that:
- _create_ws returns correct WebSocket type per exchange
- _create_ccxt returns correct ccxt client per exchange
- TradingEngine accepts exchange parameter and legacy bybit_api_key
- Unsupported exchange raises ValueError
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.engine.daemon import _create_ws, _create_ccxt, TradingEngine, SUPPORTED_EXCHANGES
from src.engine.event_bus import EventBus


class TestCreateWs:
    def test_bybit_returns_bybit_ws(self):
        bus = EventBus()
        ws = _create_ws("bybit", bus, "key", "secret")
        from src.core.websocket.bybit_ws import BybitWebSocket
        assert isinstance(ws, BybitWebSocket)

    def test_binance_returns_binance_ws(self):
        bus = EventBus()
        ws = _create_ws("binance", bus, "key", "secret")
        from src.core.websocket.binance_ws import BinanceWebSocket
        assert isinstance(ws, BinanceWebSocket)

    def test_passes_credentials(self):
        bus = EventBus()
        ws = _create_ws("bybit", bus, "my_key", "my_secret")
        assert ws._api_key == "my_key"
        assert ws._api_secret == "my_secret"


class TestCreateCcxt:
    def test_bybit_creates_bybit_client(self):
        client = _create_ccxt("bybit", "k", "s")
        assert client.id == "bybit"

    def test_binance_creates_binance_client(self):
        client = _create_ccxt("binance", "k", "s")
        assert client.id == "binance"

    def test_credentials_set(self):
        client = _create_ccxt("bybit", "test_key", "test_secret")
        assert client.apiKey == "test_key"
        assert client.secret == "test_secret"


class TestTradingEngineInit:
    def test_default_exchange_is_bybit(self):
        engine = TradingEngine(api_key="k", api_secret="s")
        assert engine._exchange_name == "bybit"

    def test_accepts_binance(self):
        engine = TradingEngine(api_key="k", api_secret="s", exchange="binance")
        assert engine._exchange_name == "binance"

    def test_case_insensitive(self):
        engine = TradingEngine(api_key="k", api_secret="s", exchange="Binance")
        assert engine._exchange_name == "binance"

    def test_unsupported_exchange_raises(self):
        with pytest.raises(ValueError, match="Unsupported exchange"):
            TradingEngine(api_key="k", api_secret="s", exchange="kraken")

    def test_legacy_bybit_api_key_compat(self):
        engine = TradingEngine(
            api_key="", api_secret="",
            bybit_api_key="legacy_key",
            bybit_api_secret="legacy_secret",
        )
        assert engine._api_key == "legacy_key"
        assert engine._api_secret == "legacy_secret"

    def test_api_key_takes_precedence_over_legacy(self):
        engine = TradingEngine(
            api_key="new_key", api_secret="new_secret",
            bybit_api_key="old_key", bybit_api_secret="old_secret",
        )
        assert engine._api_key == "new_key"
        assert engine._api_secret == "new_secret"

    def test_ws_type_is_base_class(self):
        """ws field should accept any WebSocketFeed, not just BybitWebSocket."""
        engine = TradingEngine(api_key="k", api_secret="s")
        from src.core.websocket.base import WebSocketFeed
        # ws is None before start(), but the type hint should be Optional[WebSocketFeed]
        assert engine.ws is None


class TestSupportedExchanges:
    def test_bybit_in_supported(self):
        assert "bybit" in SUPPORTED_EXCHANGES

    def test_binance_in_supported(self):
        assert "binance" in SUPPORTED_EXCHANGES
