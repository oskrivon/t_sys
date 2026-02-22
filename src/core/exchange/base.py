"""Базовый класс для адаптеров бирж."""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.core.models import (
    Balance,
    Candle,
    Exchange,
    ExchangeInfo,
    Order,
    OrderBook,
    OrderSide,
    OrderType,
    Symbol,
    Ticker,
    Timeframe,
    Trade,
)


class ExchangeAdapter(ABC):
    """Абстрактный базовый класс для адаптеров бирж.

    Все биржи реализуют этот интерфейс для унификации API.
    """

    exchange: Exchange

    @abstractmethod
    async def connect(self) -> None:
        """Подключиться к бирже и проверить авторизацию."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Отключиться от биржи."""
        ...

    @abstractmethod
    async def get_exchange_info(self) -> ExchangeInfo:
        """Получить информацию о бирже."""
        ...

    # =========================================================================
    # Market Data
    # =========================================================================

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """Получить текущий тикер для символа."""
        ...

    @abstractmethod
    async def get_tickers(self, symbols: Optional[list[str]] = None) -> list[Ticker]:
        """Получить тикеры для списка символов (или всех)."""
        ...

    @abstractmethod
    async def get_orderbook(
        self,
        symbol: str,
        limit: int = 20
    ) -> OrderBook:
        """Получить стакан ордеров."""
        ...

    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Candle]:
        """Получить исторические свечи."""
        ...

    @abstractmethod
    async def get_trades(
        self,
        symbol: str,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Получить последние сделки по символу."""
        ...

    @abstractmethod
    async def get_symbols(self) -> list[Symbol]:
        """Получить список всех торговых пар."""
        ...

    # =========================================================================
    # Account
    # =========================================================================

    @abstractmethod
    async def get_balance(self, currency: Optional[str] = None) -> list[Balance]:
        """Получить баланс (по валюте или весь)."""
        ...

    @abstractmethod
    async def get_my_trades(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Получить свои сделки."""
        ...

    # =========================================================================
    # Orders
    # =========================================================================

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
        stop_loss: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Order:
        """Создать ордер."""
        ...

    @abstractmethod
    async def cancel_order(
        self,
        order_id: str,
        symbol: str
    ) -> Order:
        """Отменить ордер."""
        ...

    @abstractmethod
    async def get_order(
        self,
        order_id: str,
        symbol: str
    ) -> Order:
        """Получить информацию об ордере."""
        ...

    @abstractmethod
    async def get_open_orders(
        self,
        symbol: Optional[str] = None
    ) -> list[Order]:
        """Получить открытые ордера."""
        ...

    @abstractmethod
    async def cancel_all_orders(
        self,
        symbol: Optional[str] = None
    ) -> list[Order]:
        """Отменить все ордера."""
        ...

    # =========================================================================
    # Helpers
    # =========================================================================

    async def create_market_buy(
        self,
        symbol: str,
        amount: Decimal,
        **kwargs,
    ) -> Order:
        """Создать рыночный ордер на покупку."""
        return await self.create_order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            amount=amount,
            **kwargs,
        )

    async def create_market_sell(
        self,
        symbol: str,
        amount: Decimal,
        **kwargs,
    ) -> Order:
        """Создать рыночный ордер на продажу."""
        return await self.create_order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            amount=amount,
            **kwargs,
        )

    async def create_limit_buy(
        self,
        symbol: str,
        amount: Decimal,
        price: Decimal,
        **kwargs,
    ) -> Order:
        """Создать лимитный ордер на покупку."""
        return await self.create_order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            amount=amount,
            price=price,
            **kwargs,
        )

    async def create_limit_sell(
        self,
        symbol: str,
        amount: Decimal,
        price: Decimal,
        **kwargs,
    ) -> Order:
        """Создать лимитный ордер на продажу."""
        return await self.create_order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            amount=amount,
            price=price,
            **kwargs,
        )
