"""Менеджер бирж — единая точка доступа ко всем биржам."""

from typing import Optional

import structlog

from src.core.config import Settings, get_settings
from src.core.exchange.base import ExchangeAdapter
from src.core.exchange.ccxt_adapter import (
    CCXTAdapter,
    create_binance_adapter,
    create_bybit_adapter,
    create_okx_adapter,
)
from src.core.models import Exchange

logger = structlog.get_logger()


class ExchangeManager:
    """Менеджер для работы с несколькими биржами.

    Пример использования:
        async with ExchangeManager() as manager:
            binance = manager.get("binance")
            ticker = await binance.get_ticker("BTC/USDT")
    """

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or get_settings()
        self._adapters: dict[Exchange, ExchangeAdapter] = {}
        self._connected = False

    async def __aenter__(self) -> "ExchangeManager":
        """Async context manager вход."""
        await self.connect_all()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager выход."""
        await self.disconnect_all()

    def _create_adapters(self) -> None:
        """Создать адаптеры для настроенных бирж."""
        if self._settings.binance.is_configured:
            self._adapters[Exchange.BINANCE] = create_binance_adapter(
                self._settings.binance
            )
            logger.debug("adapter_created", exchange="binance")

        if self._settings.bybit.is_configured:
            self._adapters[Exchange.BYBIT] = create_bybit_adapter(
                self._settings.bybit
            )
            logger.debug("adapter_created", exchange="bybit")

        if self._settings.okx.is_configured:
            self._adapters[Exchange.OKX] = create_okx_adapter(self._settings.okx)
            logger.debug("adapter_created", exchange="okx")

        # Если нет настроенных — создаём public-only адаптеры
        if not self._adapters:
            logger.warning(
                "no_configured_exchanges",
                message="Creating public-only adapters for all exchanges",
            )
            self._adapters[Exchange.BINANCE] = CCXTAdapter(
                exchange=Exchange.BINANCE,
                testnet=self._settings.binance.testnet,
            )
            self._adapters[Exchange.BYBIT] = CCXTAdapter(
                exchange=Exchange.BYBIT,
                testnet=self._settings.bybit.testnet,
            )
            self._adapters[Exchange.OKX] = CCXTAdapter(
                exchange=Exchange.OKX,
                testnet=self._settings.okx.demo,
            )

    async def connect_all(self) -> None:
        """Подключиться ко всем биржам."""
        if self._connected:
            return

        self._create_adapters()

        for exchange, adapter in self._adapters.items():
            try:
                await adapter.connect()
                logger.info("exchange_connected", exchange=exchange.value)
            except Exception as e:
                logger.error(
                    "exchange_connection_failed",
                    exchange=exchange.value,
                    error=str(e),
                )

        self._connected = True

    async def disconnect_all(self) -> None:
        """Отключиться от всех бирж."""
        for exchange, adapter in self._adapters.items():
            try:
                await adapter.disconnect()
            except Exception as e:
                logger.error(
                    "exchange_disconnect_failed",
                    exchange=exchange.value,
                    error=str(e),
                )

        self._adapters.clear()
        self._connected = False

    def get(self, exchange: str | Exchange) -> ExchangeAdapter:
        """Получить адаптер биржи.

        Args:
            exchange: Имя биржи или Exchange enum

        Returns:
            ExchangeAdapter для указанной биржи

        Raises:
            KeyError: Если биржа не найдена
        """
        if isinstance(exchange, str):
            exchange = Exchange(exchange.lower())

        if exchange not in self._adapters:
            raise KeyError(f"Exchange {exchange.value} not configured or connected")

        return self._adapters[exchange]

    @property
    def binance(self) -> ExchangeAdapter:
        """Быстрый доступ к Binance."""
        return self.get(Exchange.BINANCE)

    @property
    def bybit(self) -> ExchangeAdapter:
        """Быстрый доступ к Bybit."""
        return self.get(Exchange.BYBIT)

    @property
    def okx(self) -> ExchangeAdapter:
        """Быстрый доступ к OKX."""
        return self.get(Exchange.OKX)

    @property
    def connected_exchanges(self) -> list[Exchange]:
        """Список подключённых бирж."""
        return list(self._adapters.keys())

    def is_connected(self, exchange: str | Exchange) -> bool:
        """Проверить подключена ли биржа."""
        if isinstance(exchange, str):
            exchange = Exchange(exchange.lower())
        return exchange in self._adapters


# Singleton instance
_manager: Optional[ExchangeManager] = None


async def get_exchange_manager() -> ExchangeManager:
    """Получить глобальный ExchangeManager (singleton)."""
    global _manager
    if _manager is None:
        _manager = ExchangeManager()
        await _manager.connect_all()
    return _manager


async def close_exchange_manager() -> None:
    """Закрыть глобальный ExchangeManager."""
    global _manager
    if _manager is not None:
        await _manager.disconnect_all()
        _manager = None
