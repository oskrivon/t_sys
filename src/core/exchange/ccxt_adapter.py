"""CCXT-based адаптер для бирж."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import ccxt.async_support as ccxt
import structlog

from src.core.config import BinanceConfig, BybitConfig, OKXConfig
from src.core.exchange.base import ExchangeAdapter
from src.core.models import (
    Balance,
    Candle,
    Exchange,
    ExchangeInfo,
    MarketType,
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    Ticker,
    Timeframe,
    Trade,
)

logger = structlog.get_logger()


class CCXTAdapter(ExchangeAdapter):
    """Универсальный адаптер через CCXT.

    Работает с любой биржей, поддерживаемой CCXT.
    """

    def __init__(
        self,
        exchange: Exchange,
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        password: Optional[str] = None,  # Для OKX passphrase
        testnet: bool = True,
        **kwargs,
    ):
        self.exchange = exchange
        self._api_key = api_key
        self._secret = secret
        self._password = password
        self._testnet = testnet
        self._kwargs = kwargs

        self._client: Optional[ccxt.Exchange] = None
        self._connected = False

    @property
    def client(self) -> ccxt.Exchange:
        """Получить CCXT клиент."""
        if self._client is None:
            raise RuntimeError("Exchange not connected. Call connect() first.")
        return self._client

    async def connect(self) -> None:
        """Подключиться к бирже."""
        if self._connected:
            return

        exchange_class = getattr(ccxt, self.exchange.value)

        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }

        if self._api_key:
            config["apiKey"] = self._api_key
        if self._secret:
            config["secret"] = self._secret
        if self._password:
            config["password"] = self._password

        # Testnet настройки
        if self._testnet:
            config["sandbox"] = True

            if self.exchange == Exchange.BINANCE:
                config["options"]["defaultType"] = "spot"
                config["urls"] = {
                    "api": {
                        "public": "https://testnet.binance.vision/api/v3",
                        "private": "https://testnet.binance.vision/api/v3",
                    }
                }
            elif self.exchange == Exchange.BYBIT:
                config["options"]["testnet"] = True
            elif self.exchange == Exchange.OKX:
                # OKX использует header для demo trading
                config["headers"] = {"x-simulated-trading": "1"}

        config.update(self._kwargs)

        self._client = exchange_class(config)

        # Загружаем рынки
        await self._client.load_markets()

        # Проверяем подключение
        if self._api_key:
            try:
                await self._client.fetch_balance()
                logger.info(
                    "exchange_connected",
                    exchange=self.exchange.value,
                    testnet=self._testnet,
                    authenticated=True,
                )
            except Exception as e:
                logger.warning(
                    "exchange_auth_failed",
                    exchange=self.exchange.value,
                    error=str(e),
                )
        else:
            logger.info(
                "exchange_connected",
                exchange=self.exchange.value,
                testnet=self._testnet,
                authenticated=False,
            )

        self._connected = True

    async def disconnect(self) -> None:
        """Отключиться от биржи."""
        if self._client:
            await self._client.close()
            self._client = None
            self._connected = False
            logger.info("exchange_disconnected", exchange=self.exchange.value)

    async def get_exchange_info(self) -> ExchangeInfo:
        """Получить информацию о бирже."""
        symbols = await self.get_symbols()

        return ExchangeInfo(
            exchange=self.exchange,
            name=self.client.name,
            connected=self._connected,
            timestamp=datetime.now(timezone.utc),
            rate_limit=self.client.rateLimit,
            has_spot=self.client.has.get("spot", True),
            has_futures=self.client.has.get("future", False),
            has_margin=self.client.has.get("margin", False),
            symbols=symbols,
        )

    # =========================================================================
    # Market Data
    # =========================================================================

    async def get_ticker(self, symbol: str) -> Ticker:
        """Получить тикер."""
        data = await self.client.fetch_ticker(symbol)
        return self._parse_ticker(data)

    async def get_tickers(self, symbols: Optional[list[str]] = None) -> list[Ticker]:
        """Получить несколько тикеров."""
        data = await self.client.fetch_tickers(symbols)
        return [self._parse_ticker(t) for t in data.values()]

    async def get_orderbook(self, symbol: str, limit: int = 20) -> OrderBook:
        """Получить стакан."""
        data = await self.client.fetch_order_book(symbol, limit)
        return OrderBook(
            exchange=self.exchange,
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            bids=[(Decimal(str(p)), Decimal(str(a))) for p, a in data["bids"]],
            asks=[(Decimal(str(p)), Decimal(str(a))) for p, a in data["asks"]],
        )

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Candle]:
        """Получить свечи."""
        since_ts = int(since.timestamp() * 1000) if since else None
        data = await self.client.fetch_ohlcv(
            symbol,
            timeframe=timeframe.value,
            since=since_ts,
            limit=limit,
        )

        candles = []
        for ohlcv in data:
            candles.append(
                Candle(
                    exchange=self.exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=datetime.fromtimestamp(ohlcv[0] / 1000, tz=timezone.utc),
                    open=Decimal(str(ohlcv[1])),
                    high=Decimal(str(ohlcv[2])),
                    low=Decimal(str(ohlcv[3])),
                    close=Decimal(str(ohlcv[4])),
                    volume=Decimal(str(ohlcv[5])),
                )
            )
        return candles

    async def get_trades(
        self,
        symbol: str,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Получить публичные сделки."""
        since_ts = int(since.timestamp() * 1000) if since else None
        data = await self.client.fetch_trades(symbol, since=since_ts, limit=limit)
        return [self._parse_trade(t) for t in data]

    async def get_symbols(self) -> list[Symbol]:
        """Получить список символов."""
        symbols = []
        for market in self.client.markets.values():
            symbols.append(
                Symbol(
                    exchange=self.exchange,
                    symbol=market["symbol"],
                    base=market["base"],
                    quote=market["quote"],
                    market_type=MarketType(market.get("type", "spot")),
                    active=market.get("active", True),
                    min_amount=Decimal(str(market["limits"]["amount"]["min"]))
                    if market.get("limits", {}).get("amount", {}).get("min")
                    else None,
                    max_amount=Decimal(str(market["limits"]["amount"]["max"]))
                    if market.get("limits", {}).get("amount", {}).get("max")
                    else None,
                    min_cost=Decimal(str(market["limits"]["cost"]["min"]))
                    if market.get("limits", {}).get("cost", {}).get("min")
                    else None,
                    price_precision=market.get("precision", {}).get("price"),
                    amount_precision=market.get("precision", {}).get("amount"),
                    taker_fee=Decimal(str(market["taker"]))
                    if market.get("taker")
                    else None,
                    maker_fee=Decimal(str(market["maker"]))
                    if market.get("maker")
                    else None,
                )
            )
        return symbols

    # =========================================================================
    # Account
    # =========================================================================

    async def get_balance(self, currency: Optional[str] = None) -> list[Balance]:
        """Получить балансы."""
        data = await self.client.fetch_balance()
        balances = []

        for curr, balance in data.items():
            if curr in ("info", "timestamp", "datetime", "free", "used", "total"):
                continue

            if not isinstance(balance, dict):
                continue

            total = Decimal(str(balance.get("total", 0) or 0))
            if total == 0 and currency is None:
                continue

            if currency and curr != currency:
                continue

            balances.append(
                Balance(
                    exchange=self.exchange,
                    currency=curr,
                    timestamp=datetime.now(timezone.utc),
                    free=Decimal(str(balance.get("free", 0) or 0)),
                    used=Decimal(str(balance.get("used", 0) or 0)),
                    total=total,
                )
            )

        return balances

    async def get_my_trades(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Получить свои сделки."""
        since_ts = int(since.timestamp() * 1000) if since else None
        data = await self.client.fetch_my_trades(symbol, since=since_ts, limit=limit)
        return [self._parse_trade(t) for t in data]

    # =========================================================================
    # Orders
    # =========================================================================

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
        params = {}

        if client_order_id:
            params["clientOrderId"] = client_order_id
        if stop_price:
            params["stopPrice"] = float(stop_price)

        # Маппинг типов ордеров
        ccxt_type = order_type.value
        if order_type == OrderType.STOP_LOSS:
            ccxt_type = "stop_loss"
        elif order_type == OrderType.TAKE_PROFIT:
            ccxt_type = "take_profit"

        params.update(kwargs)

        data = await self.client.create_order(
            symbol=symbol,
            type=ccxt_type,
            side=side.value,
            amount=float(amount),
            price=float(price) if price else None,
            params=params,
        )

        return self._parse_order(data)

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        """Отменить ордер."""
        data = await self.client.cancel_order(order_id, symbol)
        return self._parse_order(data)

    async def get_order(self, order_id: str, symbol: str) -> Order:
        """Получить ордер."""
        data = await self.client.fetch_order(order_id, symbol)
        return self._parse_order(data)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        """Получить открытые ордера."""
        data = await self.client.fetch_open_orders(symbol)
        return [self._parse_order(o) for o in data]

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> list[Order]:
        """Отменить все ордера."""
        data = await self.client.cancel_all_orders(symbol)
        return [self._parse_order(o) for o in data]

    # =========================================================================
    # Futures-specific
    # =========================================================================

    async def set_leverage(self, leverage: int, symbol: str) -> None:
        """Set leverage for a futures symbol, clamped to exchange max."""
        market = self.client.market(symbol)
        max_lev = market.get("limits", {}).get("leverage", {}).get("max")
        if max_lev and leverage > max_lev:
            leverage = int(max_lev)
        try:
            await self.client.set_leverage(leverage, symbol)
        except ccxt.ExchangeError as e:
            # "leverage not modified" is not an error
            if "not modified" not in str(e):
                raise

    async def fetch_positions(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """Fetch open futures positions (raw CCXT format)."""
        return await self.client.fetch_positions(symbols)

    async def fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch current funding rate info (raw CCXT format)."""
        return await self.client.fetch_funding_rate(symbol)

    # =========================================================================
    # Parsers
    # =========================================================================

    def _parse_ticker(self, data: dict) -> Ticker:
        """Парсинг тикера из CCXT формата."""
        return Ticker(
            exchange=self.exchange,
            symbol=data["symbol"],
            timestamp=datetime.fromtimestamp(
                data["timestamp"] / 1000, tz=timezone.utc
            )
            if data.get("timestamp")
            else datetime.now(timezone.utc),
            last=Decimal(str(data["last"])) if data.get("last") else Decimal("0"),
            bid=Decimal(str(data["bid"])) if data.get("bid") else None,
            ask=Decimal(str(data["ask"])) if data.get("ask") else None,
            high_24h=Decimal(str(data["high"])) if data.get("high") else None,
            low_24h=Decimal(str(data["low"])) if data.get("low") else None,
            volume_24h=Decimal(str(data["baseVolume"]))
            if data.get("baseVolume")
            else None,
            change_24h_pct=Decimal(str(data["percentage"]))
            if data.get("percentage")
            else None,
        )

    def _parse_trade(self, data: dict) -> Trade:
        """Парсинг сделки из CCXT формата."""
        return Trade(
            id=str(data["id"]),
            exchange=self.exchange,
            symbol=data["symbol"],
            timestamp=datetime.fromtimestamp(
                data["timestamp"] / 1000, tz=timezone.utc
            ),
            side=OrderSide(data["side"]),
            price=Decimal(str(data["price"])),
            amount=Decimal(str(data["amount"])),
            cost=Decimal(str(data["cost"])) if data.get("cost") else Decimal("0"),
            fee=Decimal(str(data["fee"]["cost"]))
            if data.get("fee", {}).get("cost")
            else None,
            fee_currency=data.get("fee", {}).get("currency"),
            order_id=str(data["order"]) if data.get("order") else None,
            taker_or_maker=data.get("takerOrMaker"),
        )

    def _parse_order(self, data: dict) -> Order:
        """Парсинг ордера из CCXT формата."""
        status_map = {
            "open": OrderStatus.OPEN,
            "closed": OrderStatus.CLOSED,
            "canceled": OrderStatus.CANCELED,
            "expired": OrderStatus.EXPIRED,
            "rejected": OrderStatus.REJECTED,
        }

        return Order(
            id=str(data["id"]),
            client_order_id=data.get("clientOrderId"),
            exchange=self.exchange,
            symbol=data["symbol"],
            timestamp=datetime.fromtimestamp(
                data["timestamp"] / 1000, tz=timezone.utc
            )
            if data.get("timestamp")
            else datetime.now(timezone.utc),
            type=OrderType(data["type"].lower()) if data.get("type") and data["type"].lower() in OrderType._value2member_map_ else OrderType.MARKET,
            side=OrderSide(data["side"]) if data.get("side") else OrderSide.BUY,
            status=status_map.get(data.get("status", ""), OrderStatus.PENDING),
            price=Decimal(str(data["price"])) if data.get("price") else None,
            amount=Decimal(str(data["amount"])),
            filled=Decimal(str(data["filled"])) if data.get("filled") else Decimal("0"),
            remaining=Decimal(str(data["remaining"]))
            if data.get("remaining")
            else None,
            cost=Decimal(str(data["cost"])) if data.get("cost") else None,
            average=Decimal(str(data["average"])) if data.get("average") else None,
            fee=Decimal(str(data["fee"]["cost"]))
            if data.get("fee", {}).get("cost")
            else None,
            fee_currency=data.get("fee", {}).get("currency"),
        )


# =============================================================================
# Factory functions
# =============================================================================


def create_binance_adapter(config: BinanceConfig) -> CCXTAdapter:
    """Создать адаптер для Binance."""
    return CCXTAdapter(
        exchange=Exchange.BINANCE,
        api_key=config.api_key.get_secret_value() if config.api_key else None,
        secret=config.secret.get_secret_value() if config.secret else None,
        testnet=config.testnet,
        options={"recvWindow": config.recv_window},
    )


def create_bybit_adapter(config: BybitConfig) -> CCXTAdapter:
    """Создать адаптер для Bybit."""
    return CCXTAdapter(
        exchange=Exchange.BYBIT,
        api_key=config.api_key.get_secret_value() if config.api_key else None,
        secret=config.secret.get_secret_value() if config.secret else None,
        testnet=config.testnet,
        options={"accountType": config.account_type},
    )


def create_okx_adapter(config: OKXConfig) -> CCXTAdapter:
    """Создать адаптер для OKX."""
    return CCXTAdapter(
        exchange=Exchange.OKX,
        api_key=config.api_key.get_secret_value() if config.api_key else None,
        secret=config.secret.get_secret_value() if config.secret else None,
        password=config.passphrase.get_secret_value() if config.passphrase else None,
        testnet=config.demo,
    )
